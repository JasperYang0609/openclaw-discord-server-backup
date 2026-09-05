#!/usr/bin/env python3
"""Deterministic, loss-aware storage primitives for Discord message archives.

This module deliberately has no Discord bot client.  Callers provide API message
objects and, when binary preservation is requested, a bounded ``AssetDownloader``.
The canonical record retains independent content revisions and mutable
observations.  A checksummed ``CURRENT.json`` selects one complete generation;
individual raw/canonical trees are never selected independently.

Nothing in this module can claim full live completeness by itself.  A caller
must additionally provide a complete Discord inventory, per-entry cutoffs, live
ID coverage, and immutable pre-repair evidence to the full-history verifier.
"""
from __future__ import annotations

import fcntl
import hashlib
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


RECORD_SCHEMA = "openclaw-discord-rich-message.v1"
POINTER_SCHEMA = "openclaw-discord-rich-current.v1"
JOURNAL_SCHEMA = "openclaw-discord-rich-journal.v1"
GENERATION_MANIFEST_SCHEMA = "openclaw-discord-rich-generation.v1"
ENTRY_RECEIPT_SCHEMA = "openclaw-discord-rich-entry-receipt.v1"
TZ_TAIPEI = timezone(timedelta(hours=8))
MACHINE_MARKER_RE = re.compile(
    r"^<!-- openclaw-rich-message id=(\d{1,24}) visible=([0-9a-f]{64}) -->$",
    re.MULTILINE,
)

DEFAULT_CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
SIGNED_QUERY_KEYS = frozenset({
    "ex", "is", "hm", "sig", "signature", "token", "expires",
    "x-amz-signature", "x-amz-credential", "x-amz-date", "x-amz-expires",
    "x-amz-security-token",
})
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_NODES = 200_000
MAX_SOURCE_DEPTH = 64
MAX_STRING_BYTES = 4 * 1024 * 1024


class RichArchiveError(RuntimeError):
    """Base error for a fail-closed rich archive operation."""


class SourceBoundsError(RichArchiveError):
    pass


class SourceCensusError(RichArchiveError):
    pass


class RecordConflictError(RichArchiveError):
    pass


class AssetDownloadError(RichArchiveError):
    pass


class GenerationError(RichArchiveError):
    pass


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_timestamp(value: Any, *, field: str, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise RichArchiveError(f"missing required timestamp: {field}")
        return None
    if not isinstance(value, str):
        raise RichArchiveError(f"invalid timestamp type: {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RichArchiveError(f"invalid timestamp: {field}") from exc
    if parsed.tzinfo is None:
        raise RichArchiveError(f"timestamp must include timezone: {field}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _walk_leaves(value: Any, pointer: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        if not value:
            yield pointer or "/", value
        for key in sorted(value):
            child = f"{pointer}/{_json_pointer_escape(key)}"
            yield from _walk_leaves(value[key], child)
    elif isinstance(value, list):
        if not value:
            yield pointer or "/", value
        for index, item in enumerate(value):
            yield from _walk_leaves(item, f"{pointer}/{index}")
    else:
        yield pointer or "/", value


def sanitize_lossless_source(
    value: Any,
    *,
    max_bytes: int = MAX_SOURCE_BYTES,
    max_nodes: int = MAX_SOURCE_NODES,
    max_depth: int = MAX_SOURCE_DEPTH,
    max_string_bytes: int = MAX_STRING_BYTES,
) -> Any:
    """Return a JSON-equivalent deep copy or fail instead of truncating.

    The source contract is lossless for accepted JSON.  Bounded inputs that
    would otherwise be silently truncated are rejected before any archive write.
    """
    nodes = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise SourceBoundsError("Discord source payload exceeds node limit")
        if depth > max_depth:
            raise SourceBoundsError("Discord source payload exceeds depth limit")
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if item != item or item in (float("inf"), float("-inf")):
                raise SourceBoundsError("Discord source payload contains non-finite number")
            return item
        if isinstance(item, str):
            try:
                encoded = item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise SourceBoundsError("Discord source payload contains invalid Unicode") from exc
            if len(encoded) > max_string_bytes:
                raise SourceBoundsError("Discord source string exceeds byte limit")
            return item
        if isinstance(item, list):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, dict):
            output: dict[str, Any] = {}
            for key in sorted(item):
                if not isinstance(key, str):
                    raise SourceBoundsError("Discord source object key is not a string")
                output[key] = visit(item[key], depth + 1)
            return output
        raise SourceBoundsError(f"Discord source contains non-JSON type: {type(item).__name__}")

    result = visit(value, 0)
    if len(_json_bytes(result)) > max_bytes:
        raise SourceBoundsError("Discord source payload exceeds byte limit")
    return result


def _path_tokens(pointer: str) -> tuple[str, ...]:
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:])


VISIBLE_ROOTS = frozenset({
    "content", "components", "embeds", "poll", "sticker_items", "stickers",
    "message_snapshots", "attachments", "referenced_message", "interaction",
    "interaction_metadata", "reactions", "role_subscription_data", "call",
    "purchase_notification",
})
MUTABLE_ROOTS = frozenset({
    "reactions", "pinned", "tts", "flags", "edited_timestamp", "embeds",
})
KNOWN_COMPONENT_KEYS = frozenset({
    "type", "id", "custom_id", "style", "label", "emoji", "url", "disabled",
    "sku_id", "components", "options", "placeholder", "default_values",
    "min_values", "max_values", "min_length", "max_length", "required", "value",
    "channel_types", "content", "accessory", "media", "description", "spoiler",
    "items", "divider", "spacing", "accent_color", "component", "file", "name",
    "size", "proxy_url", "attachment_id", "height", "width", "content_type",
    "placeholder_version", "flags", "default", "animated",
})
KNOWN_EMBED_KEYS = frozenset({
    "title", "type", "description", "url", "timestamp", "color", "footer", "image",
    "thumbnail", "video", "provider", "author", "fields", "flags", "content_scan_version",
})
KNOWN_POLL_KEYS = frozenset({
    "question", "answers", "expiry", "allow_multiselect", "layout_type", "results",
})
KNOWN_POLL_NESTED_KEYS = frozenset({
    "text", "emoji", "answer_id", "poll_media", "is_finalized", "answer_counts",
    "count", "me_voted", "id", "name", "animated",
})
KNOWN_SNAPSHOT_MESSAGE_KEYS = frozenset({
    "id", "type", "content", "channel_id", "author", "attachments", "embeds",
    "mentions", "mention_roles", "mention_everyone", "timestamp", "edited_timestamp",
    "tts", "pinned", "flags", "components", "message_reference", "message_snapshots",
    "interaction", "interaction_metadata", "sticker_items", "stickers", "poll", "reactions",
})
KNOWN_EMBED_NESTED_KEYS = {
    "footer": frozenset({"text", "icon_url", "proxy_icon_url"}),
    "image": frozenset({"url", "proxy_url", "height", "width", "content_type", "placeholder", "placeholder_version", "flags"}),
    "thumbnail": frozenset({"url", "proxy_url", "height", "width", "content_type", "placeholder", "placeholder_version", "flags"}),
    "video": frozenset({"url", "proxy_url", "height", "width", "content_type", "placeholder", "placeholder_version", "flags"}),
    "provider": frozenset({"name", "url"}),
    "author": frozenset({"name", "url", "icon_url", "proxy_icon_url"}),
    "fields": frozenset({"name", "value", "inline"}),
}


def source_field_census(source: Mapping[str, Any]) -> dict[str, Any]:
    """Independent JSON-pointer census of all accepted source leaves.

    This intentionally operates on the preserved source payload, not on the
    renderer output.  Unknown fields under human-visible Discord structures are
    classified ``unknown_visible`` and make completeness fail closed.
    """
    rows: list[dict[str, str]] = []
    unknown: list[str] = []
    visible: list[str] = []
    mutable: list[str] = []

    def schema_is_known(tokens: tuple[str, ...]) -> bool:
        """Return whether a leaf under a visible structured root is known.

        This is intentionally independent of the normalizer/renderer.  A future
        Discord field remains preserved in ``apiSourcePayload`` but fails the
        visible-completeness gate until it is explicitly classified.
        """
        if not tokens:
            return True
        root = tokens[0]
        if root == "components":
            return all(
                token.isdigit() or token in KNOWN_COMPONENT_KEYS or token in {"id", "name", "animated"}
                for token in tokens[1:]
            )
        if root == "embeds":
            if len(tokens) == 1:
                return True
            if len(tokens) < 3 or tokens[1].isdigit() is False:
                return False
            top = tokens[2]
            if top not in KNOWN_EMBED_KEYS:
                return False
            if len(tokens) <= 3 or top not in KNOWN_EMBED_NESTED_KEYS:
                return True
            return all(
                token.isdigit() or token in KNOWN_EMBED_NESTED_KEYS[top]
                for token in tokens[3:]
            )
        if root == "poll":
            if len(tokens) >= 2 and tokens[1] not in KNOWN_POLL_KEYS:
                return False
            return all(
                token.isdigit() or token in KNOWN_POLL_KEYS or token in KNOWN_POLL_NESTED_KEYS
                for token in tokens[1:]
            )
        if root == "message_snapshots" and "message" in tokens:
            index = tokens.index("message")
            if len(tokens) <= index + 1:
                return True
            top = tokens[index + 1]
            if top not in KNOWN_SNAPSHOT_MESSAGE_KEYS:
                return False
            nested = (top,) + tokens[index + 2:]
            if top in {"components", "embeds", "poll", "message_snapshots"}:
                return schema_is_known(nested)
        return True

    def classify(pointer: str, value: Any) -> tuple[str, bool, bool]:
        tokens = _path_tokens(pointer)
        if not tokens:
            return "metadata", False, False
        root = tokens[0]
        visible_flag = root in VISIBLE_ROOTS or root in MUTABLE_ROOTS
        if root in {"author", "mentions"} and tokens[-1] in {"username", "global_name", "nick", "id"}:
            visible_flag = True
        mutable_flag = root in MUTABLE_ROOTS or (root == "poll" and len(tokens) >= 2 and tokens[1] == "results")
        content_flag = visible_flag and not mutable_flag
        if visible_flag and not schema_is_known(tokens):
            return "unknown_visible", content_flag, mutable_flag
        if visible_flag and mutable_flag:
            return "visible_mutable", content_flag, True
        if visible_flag:
            return "visible", content_flag, False
        return "metadata", False, False

    for pointer, value in _walk_leaves(dict(source)):
        kind, content_flag, mutable_flag = classify(pointer, value)
        rows.append({"pointer": pointer, "class": kind, "valueSha256": json_sha256(value)})
        if kind == "unknown_visible":
            unknown.append(pointer)
            visible.append(pointer)
            if mutable_flag:
                mutable.append(pointer)
        else:
            if kind in {"visible", "visible_mutable"}:
                visible.append(pointer)
            if mutable_flag:
                mutable.append(pointer)
        if content_flag:
            visible.append(pointer)
    visible = sorted(set(visible))
    mutable = sorted(set(mutable))
    content = sorted(set(pointer for pointer, _ in _walk_leaves(dict(source))) & set(visible) - set(mutable))
    return {
        "schemaVersion": "openclaw-discord-source-census.v1",
        "fields": rows,
        "fieldCount": len(rows),
        "visiblePointers": visible,
        "contentPointers": content,
        "mutablePointers": mutable,
        "unknownVisibleFields": sorted(unknown),
        "censusSha256": json_sha256(rows),
    }


def _discord_url_without_signature(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return value
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host not in DEFAULT_CDN_HOSTS:
        return value
    kept = [
        (key, val) for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in SIGNED_QUERY_KEYS
    ]
    netloc = host
    if port and port != 443:
        netloc = f"{host}:{port}"
    return urllib.parse.urlunsplit(("https", netloc, parsed.path, urllib.parse.urlencode(kept), ""))


def _stabilize_urls(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {name: _stabilize_urls(item, name) for name, item in sorted(value.items())}
    if isinstance(value, list):
        return [_stabilize_urls(item, key) for item in value]
    if key.lower() in {"url", "proxy_url", "icon_url"}:
        return _discord_url_without_signature(value)
    return value


def _split_poll(poll: Any) -> tuple[Any, Any]:
    if not isinstance(poll, dict):
        return poll, None
    content = {key: poll[key] for key in poll if key != "results"}
    mutable = {"results": poll.get("results")} if "results" in poll else None
    return content, mutable


def _content_subset(source: Mapping[str, Any]) -> dict[str, Any]:
    poll_content, _ = _split_poll(source.get("poll"))
    return _stabilize_urls({
        "type": source.get("type"),
        "content": source.get("content"),
        "author": source.get("author"),
        "mentions": source.get("mentions"),
        "mention_roles": source.get("mention_roles"),
        "mention_everyone": source.get("mention_everyone"),
        "components": source.get("components"),
        "poll": poll_content,
        "sticker_items": source.get("sticker_items"),
        "stickers": source.get("stickers"),
        "message_snapshots": source.get("message_snapshots"),
        "attachments": source.get("attachments"),
        "message_reference": source.get("message_reference"),
        "referenced_message": source.get("referenced_message"),
        "interaction": source.get("interaction"),
        "interaction_metadata": source.get("interaction_metadata"),
        "role_subscription_data": source.get("role_subscription_data"),
        "call": source.get("call"),
        "purchase_notification": source.get("purchase_notification"),
    })


def _mutable_subset(source: Mapping[str, Any]) -> dict[str, Any]:
    _, poll_mutable = _split_poll(source.get("poll"))
    return _stabilize_urls({
        "edited_timestamp": source.get("edited_timestamp"),
        "pinned": bool(source.get("pinned")),
        "tts": bool(source.get("tts")),
        "flags": source.get("flags", 0),
        "embeds": source.get("embeds"),
        "poll": poll_mutable,
        "reactions": source.get("reactions"),
    })


def _author_display(source: Mapping[str, Any]) -> str:
    author = source.get("author") if isinstance(source.get("author"), dict) else {}
    return str(author.get("global_name") or author.get("username") or author.get("id") or "unknown")


def _revision_sort_key(revision: Mapping[str, Any]) -> tuple[str, str]:
    return (str(revision.get("versionTimestamp") or ""), str(revision.get("revisionId") or ""))


def _observation_sort_key(observation: Mapping[str, Any]) -> tuple[str, str]:
    return (str(observation.get("observedAt") or ""), str(observation.get("observationId") or ""))


def _asset_url_scope(url: str, allowed_hosts: frozenset[str]) -> tuple[bool, str]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False, "invalid_url"
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or port not in (None, 443):
        return False, "not_https_443"
    if parsed.username is not None or parsed.password is not None:
        return False, "userinfo_forbidden"
    if host not in allowed_hosts:
        return False, "external_metadata_only"
    return True, "discord_cdn"


def _safe_display_filename(value: Any, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or fallback))
    text = text.replace("/", "_").replace("\\", "_").replace("\x00", "_")
    text = "".join(ch if ord(ch) >= 32 and ch not in {":", "*", "?", '"', "<", ">", "|"} else "_" for ch in text)
    text = text.strip(" .") or fallback
    while len(text.encode("utf-8")) > 180:
        text = text[:-1]
    return text or fallback


def _asset_identity(pointer: str, payload: Mapping[str, Any], url: str) -> str:
    supplied = payload.get("id") or payload.get("attachment_id")
    if supplied is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(supplied)):
        prefix = str(supplied)
    else:
        prefix = hashlib.sha256(pointer.encode("utf-8")).hexdigest()[:20]
    stable_url = _discord_url_without_signature(url)
    digest = hashlib.sha256(f"{pointer}\0{stable_url}".encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def inventory_assets(
    source: Mapping[str, Any],
    *,
    message_id: str,
    allowed_hosts: frozenset[str] = DEFAULT_CDN_HOSTS,
) -> list[dict[str, Any]]:
    """Inventory top-level and recursively nested Discord-visible assets."""
    assets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(pointer: str, payload: Mapping[str, Any], url: Any, kind: str) -> None:
        if not isinstance(url, str) or not url:
            return
        in_scope, reason = _asset_url_scope(url, allowed_hosts)
        identity = _asset_identity(pointer, payload, url)
        key = (pointer, identity)
        if key in seen:
            return
        seen.add(key)
        parsed = urllib.parse.urlsplit(url)
        path_name = PurePosixPath(parsed.path).name
        filename = _safe_display_filename(payload.get("filename") or payload.get("name") or path_name, f"asset-{identity}")
        local = f"attachments/{message_id}/{identity}--{filename}"
        size = payload.get("size")
        declared_size = int(size) if isinstance(size, int) and size >= 0 else None
        assets.append({
            "assetId": identity,
            "jsonPointer": pointer,
            "kind": kind,
            "displayFilename": filename,
            "remoteUrl": url,
            "stableRemoteUrl": _discord_url_without_signature(url),
            "declaredSize": declared_size,
            "sourceDeclaredSize": declared_size,
            "sizeSource": "discord_payload" if declared_size is not None else None,
            "contentType": payload.get("content_type") or payload.get("contentType"),
            "localRelativePath": local if in_scope else None,
            "inScope": in_scope,
            "scopeReason": reason,
            "status": "pending" if in_scope else "metadata_only",
            "byteLength": None,
            "sha256": None,
            "error": None,
        })

    def visit(value: Any, pointer: str, parent_key: str = "") -> None:
        if isinstance(value, dict):
            if parent_key == "attachments" and "url" in value:
                add(pointer, value, value.get("url"), "attachment")
            if parent_key in {"sticker_items", "stickers"}:
                if value.get("url"):
                    add(pointer, value, value.get("url"), "sticker")
                elif value.get("id"):
                    fmt = int(value.get("format_type") or 1)
                    ext = {3: "json", 4: "gif"}.get(fmt, "png")
                    add(pointer, value, f"https://cdn.discordapp.com/stickers/{value['id']}.{ext}", "sticker")
            for key in sorted(value):
                child_pointer = f"{pointer}/{_json_pointer_escape(key)}"
                child = value[key]
                if key in {"image", "thumbnail", "video", "media", "file"} and isinstance(child, dict):
                    add(child_pointer, child, child.get("url") or child.get("proxy_url"), f"{key}_media")
                elif key in {"icon_url"} and isinstance(child, str):
                    add(child_pointer, value, child, "icon_media")
                visit(child, child_pointer, key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{pointer}/{index}", parent_key)

    visit(dict(source), "")
    assets.sort(key=lambda row: (row["jsonPointer"], row["assetId"]))
    return assets


def normalize_message(
    message: Mapping[str, Any],
    *,
    expected_channel_id: str | None = None,
    observed_at: str | None = None,
    allowed_cdn_hosts: frozenset[str] = DEFAULT_CDN_HOSTS,
) -> dict[str, Any]:
    source = sanitize_lossless_source(dict(message))
    message_id = str(source.get("id") or "")
    source_channel_id = source.get("channel_id")
    if expected_channel_id is not None and source_channel_id in (None, ""):
        raise RichArchiveError(f"Discord source omitted channel_id for id {message_id}")
    channel_id = str(source_channel_id or expected_channel_id or "")
    if not message_id.isdigit():
        raise RichArchiveError("Discord message id must be numeric")
    if not channel_id.isdigit():
        raise RichArchiveError("Discord channel id must be numeric")
    if expected_channel_id is not None and channel_id != str(expected_channel_id):
        raise RichArchiveError(f"Discord message channel mismatch for id {message_id}")
    created = _iso_timestamp(source.get("timestamp"), field="timestamp", required=True)
    edited = _iso_timestamp(source.get("edited_timestamp"), field="edited_timestamp")
    observed = _iso_timestamp(observed_at, field="observed_at") or edited or created
    census = source_field_census(source)
    content = _content_subset(source)
    mutable = _mutable_subset(source)
    stable_source_hash = json_sha256(content)
    mutable_hash = json_sha256(mutable)
    revision_id = json_sha256({"versionTimestamp": edited or created, "sourcePayloadSha256": stable_source_hash})
    exact_source_hash = json_sha256(source)
    observation_id = json_sha256({
        "observedAt": observed,
        "apiSourcePayloadSha256": exact_source_hash,
        "mutableStateSha256": mutable_hash,
    })
    assets = inventory_assets(source, message_id=message_id, allowed_hosts=allowed_cdn_hosts)
    revision = {
        "revisionId": revision_id,
        "versionTimestamp": edited or created,
        "editedTimestamp": edited,
        "sourcePayloadSha256": stable_source_hash,
        "contentPayloadSha256": json_sha256(content),
        "visibleContentSha256": json_sha256(content),
        "content": content,
        "accountedVisiblePointers": census["contentPointers"],
    }
    observation = {
        "observationId": observation_id,
        "revisionId": revision_id,
        "observedAt": observed,
        "apiSourcePayload": source,
        "apiSourcePayloadSha256": exact_source_hash,
        "mutableState": mutable,
        "mutableStateSha256": mutable_hash,
        "accountedMutablePointers": census["mutablePointers"],
        "assetAllowedHosts": sorted(allowed_cdn_hosts),
        "assetInventory": assets,
    }
    visible_hash = json_sha256({"content": content, "mutable": mutable})
    return {
        "schemaVersion": RECORD_SCHEMA,
        "messageId": message_id,
        "channelId": channel_id,
        "createdTimestamp": created,
        "type": source.get("type"),
        "authorDisplay": _author_display(source),
        "contentRevisions": [revision],
        "observations": [observation],
        "activeRevisionId": revision_id,
        "activeObservationId": observation_id,
        "sourcePayloadSha256": stable_source_hash,
        "visiblePayloadSha256": visible_hash,
        "sourceCensus": census,
        "unknownVisibleFields": census["unknownVisibleFields"],
        "attachmentErrors": [],
    }


def _dedupe_by(rows: Iterable[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = str(row.get(key) or "")
        if not identity:
            raise RichArchiveError(f"missing canonical identity: {key}")
        value = dict(row)
        previous = output.get(identity)
        if previous is not None and json_sha256(previous) != json_sha256(value):
            raise RecordConflictError(f"canonical identity collision: {key}={identity}")
        output[identity] = value
    return list(output.values())


def merge_message_records(existing: Mapping[str, Any] | None, incoming: Mapping[str, Any]) -> dict[str, Any]:
    validate_record(incoming, require_assets=False)
    if existing is None:
        return dict(incoming)
    validate_record(existing, require_assets=False)
    if existing.get("messageId") != incoming.get("messageId") or existing.get("channelId") != incoming.get("channelId"):
        raise RecordConflictError("cannot merge different message/channel identities")
    revisions = _dedupe_by(
        list(existing.get("contentRevisions") or []) + list(incoming.get("contentRevisions") or []),
        "revisionId",
    )
    revisions.sort(key=_revision_sort_key)
    observations = _dedupe_by(
        list(existing.get("observations") or []) + list(incoming.get("observations") or []),
        "observationId",
    )
    observations.sort(key=_observation_sort_key)
    active_revision = revisions[-1]
    compatible_observations = [row for row in observations if row.get("revisionId") == active_revision["revisionId"]]
    if not compatible_observations:
        raise RecordConflictError("active revision has no observation")
    active_observation = compatible_observations[-1]
    source = active_observation["apiSourcePayload"]
    census = source_field_census(source)
    visible_hash = json_sha256({
        "content": active_revision["content"],
        "mutable": active_observation["mutableState"],
    })
    errors = sorted(set(existing.get("attachmentErrors") or []) | set(incoming.get("attachmentErrors") or []))
    merged = {
        "schemaVersion": RECORD_SCHEMA,
        "messageId": incoming["messageId"],
        "channelId": incoming["channelId"],
        "createdTimestamp": min(str(existing["createdTimestamp"]), str(incoming["createdTimestamp"])),
        "type": source.get("type"),
        "authorDisplay": _author_display(source),
        "contentRevisions": revisions,
        "observations": observations,
        "activeRevisionId": active_revision["revisionId"],
        "activeObservationId": active_observation["observationId"],
        "sourcePayloadSha256": active_revision["sourcePayloadSha256"],
        "visiblePayloadSha256": visible_hash,
        "sourceCensus": census,
        "unknownVisibleFields": sorted(
            set(existing.get("unknownVisibleFields") or [])
            | set(incoming.get("unknownVisibleFields") or [])
        ),
        "attachmentErrors": errors,
    }
    validate_record(merged, require_assets=False)
    return merged


def _active_parts(record: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    revisions = {row["revisionId"]: row for row in record.get("contentRevisions") or []}
    observations = {row["observationId"]: row for row in record.get("observations") or []}
    try:
        return revisions[str(record["activeRevisionId"])], observations[str(record["activeObservationId"])]
    except KeyError as exc:
        raise RichArchiveError("active revision/observation is missing") from exc


def validate_record(record: Mapping[str, Any], *, require_assets: bool = True, generation_root: Path | None = None) -> dict[str, Any]:
    """Recompute every immutable revision, mutable observation, and asset link.

    Validation deliberately walks historical rows as well as the active rows;
    otherwise corruption can hide behind a valid latest observation.
    """
    if record.get("schemaVersion") != RECORD_SCHEMA:
        raise RichArchiveError("unsupported rich message schema")
    message_id = str(record.get("messageId") or "")
    channel_id = str(record.get("channelId") or "")
    if not message_id.isdigit() or not channel_id.isdigit():
        raise RichArchiveError("invalid canonical message/channel identity")
    revisions_raw = list(record.get("contentRevisions") or [])
    observations_raw = list(record.get("observations") or [])
    if not revisions_raw or not observations_raw:
        raise RichArchiveError("canonical record has no revision or observation")
    revision_ids = [str(row.get("revisionId") or "") for row in revisions_raw]
    observation_ids = [str(row.get("observationId") or "") for row in observations_raw]
    if len(revision_ids) != len(set(revision_ids)) or len(observation_ids) != len(set(observation_ids)):
        raise RichArchiveError("duplicate revision or observation identity")
    revisions = {identity: row for identity, row in zip(revision_ids, revisions_raw)}
    observations = {identity: row for identity, row in zip(observation_ids, observations_raw)}
    linked_revisions: set[str] = set()
    unknown_fields: set[str] = set()
    attachment_errors = set(str(value) for value in (record.get("attachmentErrors") or []))
    asset_count = 0
    computed: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}

    for observation_id, observation in observations.items():
        if not re.fullmatch(r"[0-9a-f]{64}", observation_id):
            raise RichArchiveError("invalid observation identity")
        observed_at = _iso_timestamp(observation.get("observedAt"), field="observedAt", required=True)
        source = sanitize_lossless_source(observation.get("apiSourcePayload"))
        if not isinstance(source, dict):
            raise RichArchiveError("API source payload must be an object")
        if str(source.get("id") or "") != message_id or str(source.get("channel_id") or "") != channel_id:
            raise RichArchiveError("preserved source identity does not match canonical record")
        exact_hash = json_sha256(source)
        if exact_hash != observation.get("apiSourcePayloadSha256"):
            raise RichArchiveError("API source payload hash mismatch")
        content = _content_subset(source)
        mutable = _mutable_subset(source)
        mutable_hash = json_sha256(mutable)
        if mutable != observation.get("mutableState") or mutable_hash != observation.get("mutableStateSha256"):
            raise RichArchiveError("mutable observation does not match preserved source")
        expected_observation_id = json_sha256({
            "observedAt": observed_at,
            "apiSourcePayloadSha256": exact_hash,
            "mutableStateSha256": mutable_hash,
        })
        if expected_observation_id != observation_id:
            raise RichArchiveError("observation identity hash mismatch")
        revision_id = str(observation.get("revisionId") or "")
        revision = revisions.get(revision_id)
        if revision is None:
            raise RichArchiveError("observation references missing revision")
        linked_revisions.add(revision_id)
        content_hash = json_sha256(content)
        if content != revision.get("content") or content_hash != revision.get("contentPayloadSha256"):
            raise RichArchiveError("content revision does not match preserved source")
        if content_hash != revision.get("sourcePayloadSha256") or content_hash != revision.get("visibleContentSha256"):
            raise RichArchiveError("stable source fingerprint mismatch")
        version_timestamp = _iso_timestamp(revision.get("versionTimestamp"), field="versionTimestamp", required=True)
        expected_revision_id = json_sha256({
            "versionTimestamp": version_timestamp,
            "sourcePayloadSha256": content_hash,
        })
        if expected_revision_id != revision_id:
            raise RichArchiveError("revision identity hash mismatch")
        census = source_field_census(source)
        if sorted(revision.get("accountedVisiblePointers") or []) != census["contentPointers"]:
            raise SourceCensusError("content source fields are not fully accounted for")
        if sorted(observation.get("accountedMutablePointers") or []) != census["mutablePointers"]:
            raise SourceCensusError("mutable source fields are not fully accounted for")
        if set(census["contentPointers"]) | set(census["mutablePointers"]) != set(census["visiblePointers"]):
            raise SourceCensusError("visible source census has an unaccounted pointer")
        unknown_fields.update(census["unknownVisibleFields"])

        allowed_hosts = observation.get("assetAllowedHosts")
        if not isinstance(allowed_hosts, list) or not allowed_hosts or any(
            not isinstance(host, str) or host != host.lower().rstrip(".")
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            for host in allowed_hosts
        ):
            raise RichArchiveError("asset CDN allowlist receipt is invalid")
        expected_assets = {
            row["assetId"]: row for row in inventory_assets(
                source, message_id=message_id, allowed_hosts=frozenset(allowed_hosts),
            )
        }
        actual_assets = list(observation.get("assetInventory") or [])
        actual_asset_ids = [str(row.get("assetId") or "") for row in actual_assets]
        if len(actual_asset_ids) != len(set(actual_asset_ids)) or set(actual_asset_ids) != set(expected_assets):
            raise RichArchiveError("asset inventory identity mismatch")
        for asset in actual_assets:
            identity = str(asset.get("assetId") or "")
            expected_asset = expected_assets[identity]
            for key in (
                "assetId", "jsonPointer", "kind", "displayFilename", "remoteUrl",
                "stableRemoteUrl", "contentType", "localRelativePath", "inScope", "scopeReason",
            ):
                if asset.get(key) != expected_asset.get(key):
                    raise RichArchiveError(f"asset inventory source mismatch: {identity}:{key}")
            expected_size = expected_asset.get("declaredSize")
            actual_size = asset.get("declaredSize")
            if expected_size is not None:
                if actual_size != expected_size or asset.get("sizeSource") not in (None, "discord_payload"):
                    raise RichArchiveError("Discord-declared asset size was altered")
            elif actual_size is not None and (
                not isinstance(actual_size, int) or actual_size < 0 or asset.get("sizeSource") != "http_head"
            ):
                raise RichArchiveError("asset size without Discord metadata lacks verified HEAD provenance")
            if asset.get("sourceDeclaredSize") != expected_size:
                raise RichArchiveError("asset source-declared size receipt mismatch")
            asset_count += bool(asset.get("inScope"))
            if not require_assets or not asset.get("inScope"):
                continue
            if asset.get("status") != "complete" or not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256") or "")) or not isinstance(asset.get("byteLength"), int):
                attachment_errors.add(f"asset_incomplete:{identity}")
                continue
            if actual_size is not None and asset.get("byteLength") != actual_size:
                attachment_errors.add(f"asset_size_mismatch:{identity}")
            if generation_root is not None:
                path = contained_path(generation_root, str(asset.get("localRelativePath") or ""))
                if not _regular_single_link(path):
                    attachment_errors.add(f"asset_missing:{identity}")
                elif path.stat().st_size != asset["byteLength"] or file_sha256(path) != asset["sha256"]:
                    attachment_errors.add(f"asset_hash_mismatch:{identity}")
        computed[observation_id] = (content, mutable, census)

    if linked_revisions != set(revisions):
        raise RichArchiveError("content revision has no preserved source observation")
    revision, observation = _active_parts(record)
    if observation.get("revisionId") != revision.get("revisionId"):
        raise RichArchiveError("active observation does not correspond to active revision")
    content, mutable, census = computed[str(record["activeObservationId"])]
    expected_visible = json_sha256({"content": content, "mutable": mutable})
    if expected_visible != record.get("visiblePayloadSha256"):
        raise RichArchiveError("visible payload fingerprint mismatch")
    if revision.get("sourcePayloadSha256") != record.get("sourcePayloadSha256"):
        raise RichArchiveError("active source fingerprint mismatch")
    if census != record.get("sourceCensus"):
        raise SourceCensusError("active source census mismatch")
    if sorted(unknown_fields) != sorted(record.get("unknownVisibleFields") or []):
        raise SourceCensusError("unknown visible field receipt mismatch")
    if record.get("type") != observation["apiSourcePayload"].get("type"):
        raise RichArchiveError("active message type mismatch")
    return {
        "ok": not unknown_fields and not attachment_errors,
        "unknownVisibleFields": sorted(unknown_fields),
        "attachmentErrors": sorted(attachment_errors),
        "visiblePayloadSha256": expected_visible,
        "assetCount": asset_count,
    }


def _json_for_markdown(value: Any) -> str:
    # HTML escaping prevents raw HTML and image tags from executing.  JSON
    # quoting keeps arbitrary backticks/newlines inside string literals.
    return html.escape(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), quote=False)


def _section(label: str, value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return f"\n[{label}]\n\n```json\n{_json_for_markdown(value)}\n```\n"


def render_message(record: Mapping[str, Any]) -> str:
    validate_record(record, require_assets=False)
    revision, observation = _active_parts(record)
    content = revision["content"]
    mutable = observation["mutableState"]
    source = observation["apiSourcePayload"]
    timestamp = datetime.fromisoformat(str(record["createdTimestamp"]).replace("Z", "+00:00"))
    local_time = timestamp.astimezone(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S %z")
    author = html.escape(str(record.get("authorDisplay") or "unknown"), quote=False)
    message_id = str(record["messageId"])
    marker = f"<!-- openclaw-rich-message id={message_id} visible={record['visiblePayloadSha256']} -->"
    header = f"### {local_time} — {author} — id:{message_id}"
    sections = ""
    text = content.get("content")
    if isinstance(text, str) and text:
        safe_lines = [f"> {html.escape(line, quote=False)}" if line else ">" for line in text.splitlines() or [""]]
        sections += "\n[文字內容]\n\n" + "\n".join(safe_lines) + "\n"
    sections += _section("元件內容", content.get("components"))
    sections += _section("Embed", mutable.get("embeds"))
    poll = {"content": content.get("poll"), "results": mutable.get("poll")}
    if poll["content"] is not None or poll["results"] is not None:
        sections += _section("投票", poll)
    sections += _section("貼圖", content.get("sticker_items") or content.get("stickers"))
    sections += _section("轉寄快照", content.get("message_snapshots"))
    attachment_section = {
        "source": content.get("attachments"),
        "inventory": observation.get("assetInventory"),
    }
    if attachment_section["source"] or attachment_section["inventory"]:
        sections += _section("附件", attachment_section)
    reply = {"reference": content.get("message_reference"), "message": content.get("referenced_message")}
    sections += _section("回覆關係", reply if any(value is not None for value in reply.values()) else None)
    interaction = {
        key: content.get(key) for key in (
            "interaction", "interaction_metadata", "role_subscription_data", "call", "purchase_notification"
        ) if content.get(key) is not None
    }
    identity_context = {
        "author": content.get("author"),
        "mentions": content.get("mentions"),
        "mention_roles": content.get("mention_roles"),
        "mention_everyone": content.get("mention_everyone"),
    }
    if any(value not in (None, False, [], {}) for value in identity_context.values()):
        interaction["identityContext"] = identity_context
    sections += _section("互動／系統資訊", interaction)
    status = {
        "type": record.get("type"),
        "editedTimestamp": mutable.get("edited_timestamp"),
        "pinned": mutable.get("pinned"),
        "tts": mutable.get("tts"),
        "flags": mutable.get("flags"),
        "reactions": mutable.get("reactions"),
        "contentRevisionCount": len(record.get("contentRevisions") or []),
        "observationCount": len(record.get("observations") or []),
    }
    sections += _section("反應／編輯狀態", status)
    if not sections:
        census = record.get("sourceCensus") or {}
        if census.get("visiblePointers") or census.get("mutablePointers") or census.get("unknownVisibleFields"):
            raise SourceCensusError("renderer produced empty output for non-empty source census")
        sections = "\n(無文字內容)\n"
    if len(record.get("contentRevisions") or []) > 1:
        history = [
            {
                "versionTimestamp": row["versionTimestamp"],
                "editedTimestamp": row.get("editedTimestamp"),
                "sourcePayloadSha256": row["sourcePayloadSha256"],
                "content": row["content"].get("content"),
            }
            for row in sorted(record["contentRevisions"], key=_revision_sort_key)[:-1]
        ]
        sections += _section("歷史內容版本", history)
    return f"\n{marker}\n{header}\n{sections}"


def render_day(records: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(records, key=lambda row: int(str(row["messageId"])))
    return "# Discord rich message archive\n" + "\n".join(render_message(row) for row in ordered)


def parse_markdown_markers(text: str) -> list[tuple[str, str]]:
    return MACHINE_MARKER_RE.findall(text)


def required_render_sections(record: Mapping[str, Any]) -> list[str]:
    """Independently map preserved visible roots to required Markdown labels."""
    _revision, observation = _active_parts(record)
    source = observation["apiSourcePayload"]
    required: list[str] = []
    if isinstance(source.get("content"), str) and source.get("content"):
        required.append("文字內容")
    mappings = (
        ("元件內容", source.get("components")),
        ("Embed", source.get("embeds")),
        ("投票", source.get("poll")),
        ("貼圖", source.get("sticker_items") or source.get("stickers")),
        ("轉寄快照", source.get("message_snapshots")),
        ("附件", source.get("attachments") or observation.get("assetInventory")),
        ("回覆關係", source.get("message_reference") or source.get("referenced_message")),
        ("互動／系統資訊", bool(source.get("author")) or bool(source.get("mentions"))
         or bool(source.get("mention_roles")) or bool(source.get("mention_everyone"))
         or any(source.get(key) is not None for key in (
             "interaction", "interaction_metadata", "role_subscription_data", "call", "purchase_notification"
         ))),
    )
    required.extend(label for label, present in mappings if present)
    # Type/edit/pin/TTS/flags/reactions and version counters are always made
    # explicit, including otherwise-empty system messages.
    required.append("反應／編輯狀態")
    if len(record.get("contentRevisions") or []) > 1:
        required.append("歷史內容版本")
    return required


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise RichArchiveError(f"canonical JSONL is not a regular file: {path}")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RichArchiveError(f"invalid canonical JSONL line {number}: {path}") from exc
        if not isinstance(value, dict):
            raise RichArchiveError(f"canonical JSONL line is not an object: {path}:{number}")
        rows.append(value)
    return rows


def _atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    reject_symlink_path(path.parent)
    if os.path.lexists(path) and path.is_symlink():
        raise RichArchiveError(f"managed target may not be a symlink: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_dir(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")


def atomic_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    data = b"".join(_json_bytes(row) + b"\n" for row in sorted(rows, key=lambda row: int(str(row["messageId"]))))
    _atomic_bytes(path, data)


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def reject_symlink_path(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.parts[0])
    for part in absolute.parts[1:]:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise RichArchiveError(f"symlinked managed path is forbidden: {current}")


def contained_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise RichArchiveError("invalid managed relative path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RichArchiveError(f"unsafe managed relative path: {relative}")
    root_abs = root.resolve()
    candidate = root_abs.joinpath(*pure.parts)
    reject_symlink_path(root_abs)
    parent = candidate.parent
    if parent.exists():
        reject_symlink_path(parent)
    if candidate != root_abs and root_abs not in candidate.resolve(strict=False).parents:
        raise RichArchiveError(f"managed path escapes root: {relative}")
    return candidate


def _regular_single_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not path.is_symlink()


def _validated_generation_id(value: Any) -> str:
    generation_id = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", generation_id):
        raise GenerationError("unsafe generation id")
    return generation_id


def _journal_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(value)
    body.pop("journalSha256", None)
    body["journalSha256"] = json_sha256(body)
    return body


def _write_journal(path: Path, value: Mapping[str, Any]) -> None:
    atomic_json(path, _journal_payload(value))


@dataclass(frozen=True)
class AssetLimits:
    per_file_bytes: int = 64 * 1024 * 1024
    per_message_files: int = 32
    per_message_bytes: int = 256 * 1024 * 1024
    per_entry_files: int = 20_000
    per_entry_bytes: int = 20 * 1024 * 1024 * 1024
    full_run_files: int = 200_000
    full_run_bytes: int = 200 * 1024 * 1024 * 1024
    disk_reserve_bytes: int = 10 * 1024 * 1024 * 1024
    timeout_seconds: float = 30.0
    max_redirects: int = 3
    max_unknown_size_probes: int = 256
    metadata_probe_elapsed_seconds: float = 60.0


def preflight_asset_capacity(
    assets: Sequence[Mapping[str, Any]],
    destination_root: Path,
    *,
    limits: AssetLimits,
    run_file_count: int = 0,
    run_declared_bytes: int = 0,
    assume_unknown_max: bool = False,
) -> dict[str, int]:
    in_scope = [asset for asset in assets if asset.get("inScope")]
    if len(in_scope) > limits.per_entry_files or run_file_count + len(in_scope) > limits.full_run_files:
        raise AssetDownloadError("asset file quota exceeded before download")
    by_message: dict[str, list[Mapping[str, Any]]] = {}
    for asset in in_scope:
        parts = PurePosixPath(str(asset.get("localRelativePath") or "")).parts
        message_id = parts[1] if len(parts) >= 3 else ""
        by_message.setdefault(message_id, []).append(asset)
    for message_id, rows in by_message.items():
        if not message_id.isdigit() or len(rows) > limits.per_message_files:
            raise AssetDownloadError("per-message asset file quota exceeded")
    if any(asset.get("declaredSize") is None for asset in in_scope) and not assume_unknown_max:
        raise AssetDownloadError("in-scope asset is missing declared size; capacity cannot be proven")
    sizes = [
        limits.per_file_bytes if asset.get("declaredSize") is None else int(asset["declaredSize"])
        for asset in in_scope
    ]
    declared = sum(sizes)
    if any(size > limits.per_file_bytes for size in sizes):
        raise AssetDownloadError("per-file asset byte quota exceeded")
    if any(sum(
        limits.per_file_bytes if row.get("declaredSize") is None else int(row["declaredSize"])
        for row in rows
    ) > limits.per_message_bytes for rows in by_message.values()):
        raise AssetDownloadError("per-message asset byte quota exceeded")
    if declared > limits.per_entry_bytes or run_declared_bytes + declared > limits.full_run_bytes:
        raise AssetDownloadError("asset byte quota exceeded before download")
    probe = destination_root.absolute()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists() or not probe.is_dir():
        raise AssetDownloadError("no existing directory is available for disk capacity preflight")
    reject_symlink_path(probe)
    free = shutil.disk_usage(probe).free
    required = declared + limits.disk_reserve_bytes
    if free < required:
        raise AssetDownloadError("insufficient disk capacity before attachment download")
    return {"files": len(in_scope), "declaredBytes": declared, "freeBytes": free, "requiredBytes": required}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


class AssetDownloader:
    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str] = DEFAULT_CDN_HOSTS,
        limits: AssetLimits | None = None,
        opener: Any | None = None,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ) -> None:
        self.allowed_hosts = frozenset(host.lower().rstrip(".") for host in allowed_hosts)
        self.limits = limits or AssetLimits()
        self.resolver = resolver
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    def _validated_url(self, url: str, *, expected_host: str | None = None) -> tuple[str, str]:
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise AssetDownloadError("invalid asset URL") from exc
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() != "https" or port not in (None, 443):
            raise AssetDownloadError("asset URL must use HTTPS port 443")
        if parsed.username is not None or parsed.password is not None:
            raise AssetDownloadError("asset URL userinfo is forbidden")
        if not host or host not in self.allowed_hosts:
            raise AssetDownloadError("asset host is not in the exact Discord CDN allowlist")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise AssetDownloadError("asset URL IP literals are forbidden")
        if expected_host is not None and host != expected_host:
            raise AssetDownloadError("cross-host asset redirect is forbidden")
        try:
            addresses = self.resolver(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise AssetDownloadError("asset host resolution failed") from exc
        if not addresses:
            raise AssetDownloadError("asset host did not resolve")
        for row in addresses:
            address = ipaddress.ip_address(row[4][0])
            if not address.is_global:
                raise AssetDownloadError("asset host resolved to a non-global address")
        normalized = urllib.parse.urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
        return normalized, host

    def probe_size(self, asset: Mapping[str, Any]) -> int:
        """Resolve a missing Discord-declared size with a bounded safe HEAD."""
        if not asset.get("inScope"):
            return 0
        existing = asset.get("declaredSize")
        if isinstance(existing, int) and existing >= 0:
            return existing
        url, original_host = self._validated_url(str(asset.get("remoteUrl") or ""))
        visited: set[str] = set()
        redirects = 0
        while True:
            if url in visited:
                raise AssetDownloadError("asset metadata redirect loop detected")
            visited.add(url)
            request = urllib.request.Request(
                url,
                headers={"Accept-Encoding": "identity", "User-Agent": "openclaw-rich-archive/1.0"},
                method="HEAD",
            )
            try:
                response = self.opener.open(request, timeout=self.limits.timeout_seconds)
            except urllib.error.HTTPError as exc:
                if exc.code not in REDIRECT_CODES:
                    raise AssetDownloadError(f"asset metadata HTTP status {exc.code}") from exc
                location = exc.headers.get("Location")
                if not location or redirects >= self.limits.max_redirects:
                    raise AssetDownloadError("asset metadata redirect limit exceeded") from exc
                url, _ = self._validated_url(urllib.parse.urljoin(url, location), expected_host=original_host)
                redirects += 1
                continue
            try:
                status = getattr(response, "status", response.getcode())
                if status in REDIRECT_CODES:
                    location = response.headers.get("Location")
                    if not location or redirects >= self.limits.max_redirects:
                        raise AssetDownloadError("asset metadata redirect limit exceeded")
                    url, _ = self._validated_url(urllib.parse.urljoin(url, location), expected_host=original_host)
                    redirects += 1
                    continue
                if status != 200:
                    raise AssetDownloadError(f"asset metadata HTTP status {status}")
                encoding = (response.headers.get("Content-Encoding") or "identity").lower()
                if encoding != "identity":
                    raise AssetDownloadError("compressed asset metadata response is forbidden")
                length = response.headers.get("Content-Length")
                if length is None or not str(length).isdigit():
                    raise AssetDownloadError("asset metadata omitted Content-Length")
                size = int(length)
                if size > self.limits.per_file_bytes:
                    raise AssetDownloadError("asset exceeds per-file size limit")
                return size
            finally:
                response.close()

    def download(self, asset: Mapping[str, Any], generation_root: Path) -> dict[str, Any]:
        row = dict(asset)
        if not row.get("inScope"):
            return row
        target = contained_path(generation_root, str(row.get("localRelativePath") or ""))
        expected_size = row.get("declaredSize")
        if not isinstance(expected_size, int) or expected_size < 0:
            raise AssetDownloadError("asset declared size is required")
        if expected_size > self.limits.per_file_bytes:
            raise AssetDownloadError("asset exceeds per-file size limit")
        url, original_host = self._validated_url(str(row.get("remoteUrl") or ""))
        if _regular_single_link(target) and row.get("sha256") and row.get("byteLength") == target.stat().st_size:
            if file_sha256(target) == row["sha256"]:
                row.update({"status": "complete", "error": None})
                return row
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        reject_symlink_path(target.parent)
        visited: set[str] = set()
        redirects = 0
        while True:
            if url in visited:
                raise AssetDownloadError("asset redirect loop detected")
            visited.add(url)
            request = urllib.request.Request(
                url,
                headers={"Accept-Encoding": "identity", "User-Agent": "openclaw-rich-archive/1.0"},
                method="GET",
            )
            try:
                response = self.opener.open(request, timeout=self.limits.timeout_seconds)
            except urllib.error.HTTPError as exc:
                if exc.code not in REDIRECT_CODES:
                    raise AssetDownloadError(f"asset HTTP status {exc.code}") from exc
                location = exc.headers.get("Location")
                if not location or redirects >= self.limits.max_redirects:
                    raise AssetDownloadError("asset redirect limit exceeded") from exc
                url, _ = self._validated_url(urllib.parse.urljoin(url, location), expected_host=original_host)
                redirects += 1
                continue
            status = getattr(response, "status", response.getcode())
            if status in REDIRECT_CODES:
                location = response.headers.get("Location")
                response.close()
                if not location or redirects >= self.limits.max_redirects:
                    raise AssetDownloadError("asset redirect limit exceeded")
                url, _ = self._validated_url(urllib.parse.urljoin(url, location), expected_host=original_host)
                redirects += 1
                continue
            if status != 200:
                response.close()
                raise AssetDownloadError(f"asset HTTP status {status}")
            encoding = (response.headers.get("Content-Encoding") or "identity").lower()
            if encoding != "identity":
                response.close()
                raise AssetDownloadError("compressed asset response is forbidden")
            content_length = response.headers.get("Content-Length")
            if content_length is None or not content_length.isdigit() or int(content_length) != expected_size:
                response.close()
                raise AssetDownloadError("asset Content-Length does not match declared size")
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".download", dir=target.parent)
            digest = hashlib.sha256()
            count = 0
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > self.limits.per_file_bytes or count > expected_size:
                            raise AssetDownloadError("asset stream exceeded declared or configured size")
                        handle.write(chunk)
                        digest.update(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if count != expected_size:
                    raise AssetDownloadError("asset stream was truncated")
                os.replace(temporary, target)
                os.chmod(target, 0o600)
                _fsync_dir(target.parent)
            finally:
                response.close()
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            row.update({"status": "complete", "byteLength": count, "sha256": digest.hexdigest(), "error": None})
            return row


def resolve_asset_sizes(record: Mapping[str, Any], downloader: AssetDownloader) -> dict[str, Any]:
    """Fill missing in-scope sizes from safe HEAD metadata before mutation."""
    updated = dict(record)
    observations = [dict(row) for row in updated.get("observations") or []]
    unknown_count = sum(
        1 for observation in observations for asset in observation.get("assetInventory") or []
        if asset.get("inScope") and asset.get("declaredSize") is None
    )
    if unknown_count > downloader.limits.max_unknown_size_probes:
        raise AssetDownloadError("unknown-size asset metadata probe quota exceeded")
    deadline = time.monotonic() + downloader.limits.metadata_probe_elapsed_seconds
    for observation in observations:
        output: list[dict[str, Any]] = []
        for asset in observation.get("assetInventory") or []:
            row = dict(asset)
            if row.get("inScope") and row.get("declaredSize") is None:
                if time.monotonic() > deadline:
                    raise AssetDownloadError("asset metadata probe elapsed-time cap exceeded")
                row["declaredSize"] = downloader.probe_size(row)
                row["sizeSource"] = "http_head"
            output.append(row)
        observation["assetInventory"] = output
    updated["observations"] = observations
    return updated


def apply_asset_results(record: Mapping[str, Any], downloader: AssetDownloader, generation_root: Path) -> dict[str, Any]:
    updated = dict(record)
    observations = [dict(row) for row in updated.get("observations") or []]
    errors: list[str] = []
    for observation in observations:
        output: list[dict[str, Any]] = []
        for asset in observation.get("assetInventory") or []:
            try:
                output.append(downloader.download(asset, generation_root))
            except AssetDownloadError as exc:
                failed = dict(asset)
                failed.update({"status": "error", "error": str(exc)})
                output.append(failed)
                errors.append(f"{asset.get('assetId')}:{exc}")
        observation["assetInventory"] = output
    updated["observations"] = observations
    updated["attachmentErrors"] = sorted(set(errors))
    return updated


def canonical_day(message: Mapping[str, Any]) -> str:
    value = _iso_timestamp(message.get("timestamp"), field="timestamp", required=True)
    assert value is not None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ_TAIPEI).date().isoformat()


def merge_day_records(existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for record in list(existing) + list(incoming):
        message_id = str(record.get("messageId") or "")
        by_id[message_id] = merge_message_records(by_id.get(message_id), record)
    return [by_id[key] for key in sorted(by_id, key=int)]


def generation_inventory(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    if root.is_symlink() or not root.is_dir():
        raise GenerationError("generation root must be a regular directory")
    for current, dirs, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in dirs:
            child = current_path / name
            if child.is_symlink() or not child.is_dir():
                raise GenerationError("generation contains invalid directory")
        for name in names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if relative == "generation-manifest.json":
                continue
            if not _regular_single_link(path):
                raise GenerationError("generation contains non-regular file")
            files.append({"path": relative, "bytes": path.stat().st_size, "sha256": file_sha256(path)})
    files.sort(key=lambda row: row["path"])
    content_files = [row for row in files if not row["path"].startswith("receipts/")]
    return {
        "schemaVersion": GENERATION_MANIFEST_SCHEMA,
        "files": files,
        "fileCount": len(files),
        "bytes": sum(row["bytes"] for row in files),
        "contentGenerationSha256": json_sha256(content_files),
        "generationSha256": json_sha256(files),
    }


def _coverage_is_complete(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) == 100.0
    return isinstance(value, str) and value.strip() in {"100", "100%", "100.0", "100.0%"}


def _full_gate_errors(receipt: Mapping[str, Any], content_generation_sha256: str) -> list[str]:
    errors: list[str] = []
    for key in (
        "inventoryCoverage", "idCoverage", "visibleTextCoverage",
        "markdownCoverage", "binaryAssetCoverage",
    ):
        if not _coverage_is_complete(receipt.get(key)):
            errors.append(f"{key}_not_100_percent")
    for key in ("liveErrors", "duplicateCanonicalIds", "unknownVisibleFields", "attachmentErrors"):
        if receipt.get(key) != 0:
            errors.append(f"{key}_not_zero")
    if receipt.get("inventoryComplete") is not True:
        errors.append("inventory_not_complete")
    if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("inventoryDigest") or "")):
        errors.append("inventory_digest_invalid")
    cutoff = receipt.get("verifiedCutoff")
    if not isinstance(cutoff, str) or not cutoff.isdigit():
        errors.append("verified_cutoff_invalid")
    evidence = receipt.get("immutableEvidenceVerified")
    if evidence not in (True, "PASS"):
        errors.append("immutable_evidence_not_verified")
    if receipt.get("contentGenerationSha256") != content_generation_sha256:
        errors.append("content_generation_hash_mismatch")
    return errors


def verify_generation(root: Path, *, require_full_gate: bool = False) -> dict[str, Any]:
    manifest_path = root / "generation-manifest.json"
    if not _regular_single_link(manifest_path):
        raise GenerationError("generation manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = generation_inventory(root)
    if manifest != actual:
        raise GenerationError("generation manifest mismatch")
    duplicate_ids = 0
    unknown = 0
    asset_errors = 0
    markdown_errors = 0
    section_coverage_errors = 0
    records_count = 0
    canonical_days: set[str] = set()
    for canonical_path in sorted((root / "canonical").glob("*.jsonl")) if (root / "canonical").is_dir() else []:
        canonical_days.add(canonical_path.stem)
        rows = load_jsonl(canonical_path)
        ids = [str(row.get("messageId")) for row in rows]
        duplicate_ids += len(ids) - len(set(ids))
        for row in rows:
            outcome = validate_record(row, generation_root=root)
            unknown += len(outcome["unknownVisibleFields"])
            asset_errors += len(outcome["attachmentErrors"])
        raw_path = root / "raw" / f"{canonical_path.stem}.md"
        expected_bytes = render_day(rows).encode("utf-8")
        actual_bytes = raw_path.read_bytes() if _regular_single_link(raw_path) else b""
        markers = parse_markdown_markers(actual_bytes.decode("utf-8") if actual_bytes else "")
        expected = [(str(row["messageId"]), str(row["visiblePayloadSha256"])) for row in rows]
        if markers != expected or actual_bytes != expected_bytes:
            markdown_errors += 1
        for row in rows:
            rendered = render_message(row)
            for label in required_render_sections(row):
                if f"\n[{label}]\n" not in rendered:
                    section_coverage_errors += 1
        records_count += len(rows)
    raw_days = {
        path.stem for path in (root / "raw").glob("*.md")
        if _regular_single_link(path)
    } if (root / "raw").is_dir() else set()
    if raw_days != canonical_days:
        markdown_errors += 1
    receipt_path = root / "receipts" / "rich-archive-latest.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if _regular_single_link(receipt_path) else {}
    required_full_fields = {
        "inventoryCoverage", "idCoverage", "visibleTextCoverage", "markdownCoverage",
        "binaryAssetCoverage", "unknownVisibleFields", "attachmentErrors", "verifiedCutoff",
        "immutableEvidenceVerified", "liveErrors", "duplicateCanonicalIds",
        "inventoryComplete", "inventoryDigest", "contentGenerationSha256",
    }
    full_gate_present = required_full_fields.issubset(receipt)
    full_gate_errors = _full_gate_errors(receipt, actual["contentGenerationSha256"]) if full_gate_present else ["full_gate_fields_missing"]
    if receipt.get("gateStatus") == "PASS" and full_gate_errors:
        raise GenerationError("PASS receipt failed exact full live completeness validation")
    if require_full_gate and (
        not full_gate_present or receipt.get("gateStatus") != "PASS" or full_gate_errors
    ):
        raise GenerationError("full live completeness gate is absent or not PASS")
    ok = (
        duplicate_ids == 0 and unknown == 0 and asset_errors == 0
        and markdown_errors == 0 and section_coverage_errors == 0
    )
    return {
        "ok": ok,
        "verified": ok,
        "gateStatus": receipt.get("gateStatus", "INCOMPLETE"),
        "fullGatePresent": full_gate_present,
        "fullGateErrors": full_gate_errors,
        "records": records_count,
        "duplicateCanonicalIds": duplicate_ids,
        "unknownVisibleFields": unknown,
        "attachmentErrors": asset_errors,
        "markdownErrors": markdown_errors,
        "sectionCoverageErrors": section_coverage_errors,
        "generationSha256": actual["generationSha256"],
    }


class RichArchiveStore:
    """Versioned per-entry archive with atomic CURRENT pointer publication."""

    def __init__(self, entry_root: Path, *, lock_path: Path | None = None) -> None:
        self.entry_root = entry_root.absolute()
        self.generations = self.entry_root / "generations"
        self.pointer_path = self.entry_root / "CURRENT.json"
        self.journal_path = self.entry_root / ".rich-archive-transaction.json"
        self.lock_path = lock_path

    def acquire_lock(self):
        if self.lock_path is None:
            return None
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        reject_symlink_path(self.lock_path.parent)
        if not hasattr(os, "O_NOFOLLOW"):
            raise RichArchiveError("platform lacks O_NOFOLLOW for shared backup lock")
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise RichArchiveError("shared backup lock cannot be opened safely") from exc
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            os.close(descriptor)
            raise RichArchiveError("shared backup lock failed regular-file owner/link/mode gate")
        handle = os.fdopen(descriptor, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RichArchiveError("shared backup lock is busy")
        return handle

    def _pointer_body(self, generation_id: str, generation_sha256: str) -> dict[str, str]:
        return {
            "schemaVersion": POINTER_SCHEMA,
            "generationId": generation_id,
            "generationSha256": generation_sha256,
        }

    def resolve_current(self) -> Path | None:
        if not self.pointer_path.exists():
            return None
        if not _regular_single_link(self.pointer_path):
            raise GenerationError("CURRENT pointer is not a regular file")
        pointer = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        checksum = pointer.pop("pointerSha256", None)
        if checksum != json_sha256(pointer) or pointer.get("schemaVersion") != POINTER_SCHEMA:
            raise GenerationError("CURRENT pointer checksum or schema mismatch")
        generation_id = _validated_generation_id(pointer.get("generationId"))
        root = contained_path(self.generations, generation_id)
        result = verify_generation(root)
        if result["generationSha256"] != pointer.get("generationSha256"):
            raise GenerationError("CURRENT generation hash mismatch")
        return root

    def create_stage(self, generation_id: str, *, copy_current: bool = True) -> Path:
        generation_id = _validated_generation_id(generation_id)
        self.generations.mkdir(parents=True, exist_ok=True, mode=0o700)
        reject_symlink_path(self.generations)
        stage = self.generations / f".staging-{generation_id}"
        if os.path.lexists(stage):
            if stage.is_symlink() or not stage.is_dir():
                raise GenerationError("invalid existing stage")
            return stage
        current = self.resolve_current() if copy_current else None
        if current is not None:
            shutil.copytree(current, stage, copy_function=shutil.copy2)
            (stage / "generation-manifest.json").unlink(missing_ok=True)
        else:
            stage.mkdir(mode=0o700)
        for name in ("canonical", "raw", "attachments", "receipts", "legacy-retained"):
            (stage / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        return stage

    def merge_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        channel_id: str,
        observed_at: str,
        generation_id: str,
        downloader: AssetDownloader | None = None,
        lock_already_held: bool = False,
    ) -> dict[str, Any]:
        lock = None if lock_already_held else self.acquire_lock()
        try:
            if self.resolve_current() is None:
                raise GenerationError("rich archive CURRENT generation is missing; full rebuild is required")
            normalized = [
                normalize_message(message, expected_channel_id=channel_id, observed_at=observed_at,
                                  allowed_cdn_hosts=(downloader.allowed_hosts if downloader else DEFAULT_CDN_HOSTS))
                for message in messages
            ]
            if downloader is not None:
                assets = [
                    asset for record in normalized for observation in record["observations"]
                    for asset in observation["assetInventory"]
                ]
                preflight_asset_capacity(
                    assets, self.entry_root, limits=downloader.limits,
                    assume_unknown_max=True,
                )
                normalized = [resolve_asset_sizes(record, downloader) for record in normalized]
                assets = [
                    asset for record in normalized for observation in record["observations"]
                    for asset in observation["assetInventory"]
                ]
                preflight_asset_capacity(assets, self.entry_root, limits=downloader.limits)
            stage = self.create_stage(generation_id, copy_current=True)
            by_day: dict[str, list[dict[str, Any]]] = {}
            for message, record in zip(messages, normalized):
                by_day.setdefault(canonical_day(message), []).append(record)
            if downloader is not None:
                normalized = [apply_asset_results(record, downloader, stage) for record in normalized]
                by_day.clear()
                for message, record in zip(messages, normalized):
                    by_day.setdefault(canonical_day(message), []).append(record)
            for day, incoming in by_day.items():
                canonical_path = stage / "canonical" / f"{day}.jsonl"
                merged = merge_day_records(load_jsonl(canonical_path), incoming)
                atomic_jsonl(canonical_path, merged)
                _atomic_bytes(stage / "raw" / f"{day}.md", render_day(merged).encode("utf-8"))
            receipt = {
                "schemaVersion": ENTRY_RECEIPT_SCHEMA,
                "gateStatus": "INCOMPLETE",
                "reason": "full_live_inventory_and_cutoff_gate_not_supplied",
                "messageCount": sum(len(load_jsonl(path)) for path in (stage / "canonical").glob("*.jsonl")),
                "unknownVisibleFields": sum(
                    len(row.get("unknownVisibleFields") or [])
                    for path in (stage / "canonical").glob("*.jsonl") for row in load_jsonl(path)
                ),
                "attachmentErrors": sum(
                    len(row.get("attachmentErrors") or [])
                    for path in (stage / "canonical").glob("*.jsonl") for row in load_jsonl(path)
                ),
            }
            atomic_json(stage / "receipts" / "rich-archive-latest.json", receipt)
            manifest = generation_inventory(stage)
            atomic_json(stage / "generation-manifest.json", manifest)
            local = verify_generation(stage)
            if not local["ok"]:
                raise GenerationError("staged generation failed local verification")
            self.publish_stage(stage, generation_id, manifest["generationSha256"])
            return {"generationId": generation_id, "verified": True, **local}
        finally:
            if lock is not None:
                lock.close()

    def publish_stage(
        self,
        stage: Path,
        generation_id: str,
        generation_sha256: str,
        *,
        require_full_gate: bool = False,
    ) -> None:
        generation_id = _validated_generation_id(generation_id)
        if not re.fullmatch(r"[0-9a-f]{64}", generation_sha256):
            raise GenerationError("invalid generation checksum")
        final = contained_path(self.generations, generation_id)
        expected_stage = contained_path(self.generations, f".staging-{generation_id}")
        if stage.absolute() != expected_stage.absolute() or stage.is_symlink() or not stage.is_dir():
            raise GenerationError("publish stage path is invalid")
        verified = verify_generation(stage, require_full_gate=require_full_gate)
        if not verified["ok"] or verified["generationSha256"] != generation_sha256:
            raise GenerationError("publish stage failed generation verification")
        if final.exists() or final.is_symlink():
            raise GenerationError("generation destination already exists")
        journal = {
            "schemaVersion": JOURNAL_SCHEMA,
            "phase": "prepared",
            "generationId": generation_id,
            "generationSha256": generation_sha256,
            "previousPointerSha256": file_sha256(self.pointer_path) if self.pointer_path.exists() else None,
        }
        _write_journal(self.journal_path, journal)
        os.replace(stage, final)
        _fsync_dir(self.generations)
        journal["phase"] = "generation_ready"
        _write_journal(self.journal_path, journal)
        pointer = self._pointer_body(generation_id, generation_sha256)
        pointer["pointerSha256"] = json_sha256(pointer)
        atomic_json(self.pointer_path, pointer)
        journal["phase"] = "committed"
        journal["committedPointerSha256"] = file_sha256(self.pointer_path)
        _write_journal(self.journal_path, journal)

    def recover_journal(self) -> dict[str, Any]:
        if not self.journal_path.exists():
            return {"phase": "none", "action": "none"}
        if not _regular_single_link(self.journal_path):
            raise GenerationError("transaction journal is invalid")
        journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
        checksum = journal.pop("journalSha256", None)
        if checksum != json_sha256(journal) or journal.get("schemaVersion") != JOURNAL_SCHEMA:
            raise GenerationError("unsupported transaction journal")
        phase = journal.get("phase")
        if phase not in {"prepared", "generation_ready", "committed"}:
            raise GenerationError("transaction journal phase is invalid")
        generation_id = _validated_generation_id(journal.get("generationId"))
        generation_sha256 = str(journal.get("generationSha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", generation_sha256):
            raise GenerationError("transaction journal generation checksum is invalid")
        final = contained_path(self.generations, generation_id)
        current = self.resolve_current()
        if phase == "committed":
            if current is None or current.name != generation_id:
                raise GenerationError("committed journal does not match CURRENT")
            if verify_generation(current)["generationSha256"] != generation_sha256:
                raise GenerationError("committed journal generation hash mismatch")
            return {"phase": phase, "action": "none"}
        if current is not None and current.name == generation_id:
            if verify_generation(current)["generationSha256"] != generation_sha256:
                raise GenerationError("recovered CURRENT generation hash mismatch")
            journal["phase"] = "committed"
            journal["recovered"] = True
            _write_journal(self.journal_path, journal)
            return {"phase": "committed", "action": "finalized_pointer_commit"}
        # A complete but unpublished generation is retained for explicit resume;
        # automatic recovery never changes CURRENT.
        if final.exists():
            if verify_generation(final)["generationSha256"] != generation_sha256:
                raise GenerationError("unpublished generation hash mismatch")
            return {"phase": phase, "action": "retain_unpublished_generation"}
        return {"phase": phase, "action": "retain_old_current"}


__all__ = [
    "AssetDownloadError", "AssetDownloader", "AssetLimits", "DEFAULT_CDN_HOSTS",
    "GenerationError", "RECORD_SCHEMA", "RichArchiveError", "RichArchiveStore",
    "SourceBoundsError", "SourceCensusError", "apply_asset_results", "atomic_json",
    "atomic_jsonl", "canonical_day", "contained_path", "file_sha256",
    "generation_inventory", "inventory_assets", "json_sha256", "load_jsonl",
    "merge_day_records", "merge_message_records", "normalize_message",
    "parse_markdown_markers", "preflight_asset_capacity", "render_day",
    "render_message", "required_render_sections", "resolve_asset_sizes",
    "sanitize_lossless_source", "source_field_census",
    "validate_record", "verify_generation",
]
