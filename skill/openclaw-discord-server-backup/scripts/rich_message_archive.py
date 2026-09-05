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
import ipaddress
import json
import os
import re
import secrets
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
ENTRY_RECEIPT_SCHEMA = "openclaw-discord-rich-entry-receipt.v2"
LIVE_EVIDENCE_SCHEMA = "openclaw-discord-rich-live-evidence.v1"
SOURCE_CENSUS_SCHEMA = "openclaw-discord-source-census.v2"
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


# Every accepted top-level Discord message field is explicitly classified.  A
# future root is treated as potentially visible and fails the full gate until a
# maintainer deliberately assigns it a policy and renderer section.
ROOT_POLICIES: dict[str, tuple[str, str | None]] = {
    "id": ("metadata", None),
    "channel_id": ("metadata", None),
    "guild_id": ("metadata", None),
    "timestamp": ("metadata", None),
    "webhook_id": ("metadata", None),
    "nonce": ("metadata", None),
    "position": ("metadata", None),
    "type": ("visible_mutable", "反應／編輯狀態"),
    "content": ("visible_content", "文字內容"),
    "author": ("visible_content", "互動／系統資訊"),
    "member": ("visible_content", "互動／系統資訊"),
    "mentions": ("visible_content", "互動／系統資訊"),
    "mention_roles": ("visible_content", "互動／系統資訊"),
    "mention_everyone": ("visible_content", "互動／系統資訊"),
    "mention_channels": ("visible_content", "互動／系統資訊"),
    "attachments": ("visible_content", "附件"),
    "components": ("visible_content", "元件內容"),
    "sticker_items": ("visible_content", "貼圖"),
    "stickers": ("visible_content", "貼圖"),
    "message_snapshots": ("visible_content", "轉寄快照"),
    "message_reference": ("visible_content", "回覆關係"),
    "referenced_message": ("visible_content", "回覆關係"),
    "interaction": ("visible_content", "互動／系統資訊"),
    "interaction_metadata": ("visible_content", "互動／系統資訊"),
    "role_subscription_data": ("visible_content", "互動／系統資訊"),
    "purchase_notification": ("visible_content", "互動／系統資訊"),
    "call": ("visible_content", "互動／系統資訊"),
    "activity": ("visible_content", "互動／系統資訊"),
    "application": ("visible_content", "互動／系統資訊"),
    "application_id": ("visible_content", "互動／系統資訊"),
    "thread": ("visible_content", "互動／系統資訊"),
    "resolved": ("visible_content", "互動／系統資訊"),
    "edited_timestamp": ("visible_mutable", "反應／編輯狀態"),
    "pinned": ("visible_mutable", "反應／編輯狀態"),
    "tts": ("visible_mutable", "反應／編輯狀態"),
    "flags": ("visible_mutable", "反應／編輯狀態"),
    "embeds": ("visible_mutable", "Embed"),
    "reactions": ("visible_mutable", "反應／編輯狀態"),
    "poll": ("visible_mixed", "投票"),
}
VISIBLE_ROOTS = frozenset(
    root for root, (policy, _section_name) in ROOT_POLICIES.items()
    if policy != "metadata"
)
MUTABLE_ROOTS = frozenset(
    root for root, (policy, _section_name) in ROOT_POLICIES.items()
    if policy == "visible_mutable"
)
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
    rows: list[dict[str, Any]] = []
    unknown: list[str] = []
    visible: list[str] = []
    mutable: list[str] = []
    content: list[str] = []
    unclassified: list[str] = []

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

    def classify(pointer: str, value: Any) -> tuple[str, bool, bool, str | None]:
        tokens = _path_tokens(pointer)
        if not tokens:
            return "metadata", False, False, None
        root = tokens[0]
        policy_row = ROOT_POLICIES.get(root)
        if policy_row is None:
            return "unclassified", False, False, "未分類可見資料"
        policy, section_name = policy_row
        if policy == "metadata":
            return "metadata", False, False, None
        mutable_flag = policy == "visible_mutable" or (
            policy == "visible_mixed" and len(tokens) >= 2 and tokens[1] == "results"
        )
        content_flag = not mutable_flag
        if not schema_is_known(tokens):
            return "unknown_visible", content_flag, mutable_flag, section_name
        if mutable_flag:
            return "visible_mutable", False, True, section_name
        return "visible_content", True, False, section_name

    for pointer, value in _walk_leaves(dict(source)):
        kind, content_flag, mutable_flag, section_name = classify(pointer, value)
        rows.append({
            "pointer": pointer,
            "class": kind,
            "rendererSection": section_name,
            "valueSha256": json_sha256(value),
        })
        if kind == "unclassified":
            unknown.append(pointer)
            unclassified.append(pointer)
            visible.append(pointer)
        if kind == "unknown_visible":
            unknown.append(pointer)
            visible.append(pointer)
            if mutable_flag:
                mutable.append(pointer)
        else:
            if kind in {"visible_content", "visible_mutable"}:
                visible.append(pointer)
            if mutable_flag:
                mutable.append(pointer)
        if content_flag:
            content.append(pointer)
    visible = sorted(set(visible))
    mutable = sorted(set(mutable))
    content = sorted(set(content))
    return {
        "schemaVersion": SOURCE_CENSUS_SCHEMA,
        "fields": rows,
        "fieldCount": len(rows),
        "visiblePointers": visible,
        "contentPointers": content,
        "mutablePointers": mutable,
        "unclassifiedPointers": sorted(set(unclassified)),
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
    if key.lower() == "url" or key.lower().endswith("_url"):
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
    subset = {
        root: source.get(root)
        for root, (policy, _section_name) in ROOT_POLICIES.items()
        if policy == "visible_content"
    }
    subset["poll"] = poll_content
    return _stabilize_urls(subset)


def _mutable_subset(source: Mapping[str, Any]) -> dict[str, Any]:
    _, poll_mutable = _split_poll(source.get("poll"))
    subset = {
        root: source.get(root)
        for root, (policy, _section_name) in ROOT_POLICIES.items()
        if policy == "visible_mutable"
    }
    subset["poll"] = poll_mutable
    return _stabilize_urls(subset)


def renderer_pointer_accounting(
    census: Mapping[str, Any],
    pointers: Iterable[str],
    *,
    source: Mapping[str, Any] | None = None,
    stabilize_urls: bool = False,
) -> list[dict[str, str]]:
    """Bind each visible source leaf/value to its deterministic Markdown section."""
    wanted = set(pointers)
    value_hashes: dict[str, str] = {}
    if source is not None:
        accounting_source: Any = _stabilize_urls(source) if stabilize_urls else dict(source)
        value_hashes = {
            pointer: json_sha256(value)
            for pointer, value in _walk_leaves(accounting_source)
        }
    rows: list[dict[str, str]] = []
    for field in census.get("fields") or []:
        pointer = str(field.get("pointer") or "")
        if pointer not in wanted:
            continue
        section_name = field.get("rendererSection")
        if not isinstance(section_name, str) or not section_name:
            raise SourceCensusError(f"visible pointer has no renderer section: {pointer}")
        rows.append({
            "pointer": pointer,
            "valueSha256": value_hashes.get(pointer, str(field.get("valueSha256") or "")),
            "rendererSection": section_name,
        })
    rows.sort(key=lambda row: row["pointer"])
    if {row["pointer"] for row in rows} != wanted:
        raise SourceCensusError("renderer accounting does not cover every requested pointer")
    return rows


def _author_display(source: Mapping[str, Any]) -> str:
    author = source.get("author") if isinstance(source.get("author"), dict) else {}
    return str(author.get("global_name") or author.get("username") or author.get("id") or "unknown")


def _revision_sort_key(revision: Mapping[str, Any]) -> tuple[str, str]:
    return (str(revision.get("versionTimestamp") or ""), str(revision.get("revisionId") or ""))


def _observation_sort_key(observation: Mapping[str, Any]) -> tuple[str, str]:
    return (str(observation.get("observedAt") or ""), str(observation.get("observationId") or ""))


def _reject_equal_timestamp_conflicts(
    rows: Sequence[Mapping[str, Any]],
    *,
    timestamp_key: str,
    identity_key: str,
) -> None:
    identities_by_timestamp: dict[str, set[str]] = {}
    for row in rows:
        timestamp = str(row.get(timestamp_key) or "")
        identity = str(row.get(identity_key) or "")
        identities_by_timestamp.setdefault(timestamp, set()).add(identity)
    if any(len(identities) != 1 for identities in identities_by_timestamp.values()):
        raise RecordConflictError(f"conflicting {identity_key} values share one {timestamp_key}")


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
            if parent_key == "attachments":
                add(f"{pointer}/url", value, value.get("url"), "attachment")
                if value.get("proxy_url") and value.get("proxy_url") != value.get("url"):
                    add(f"{pointer}/proxy_url", value, value.get("proxy_url"), "attachment_proxy")
            if parent_key in {"sticker_items", "stickers"}:
                if value.get("url"):
                    add(f"{pointer}/url", value, value.get("url"), "sticker")
                elif value.get("id"):
                    fmt = int(value.get("format_type") or 1)
                    ext = {3: "json", 4: "gif"}.get(fmt, "png")
                    add(f"{pointer}/derived_url", value, f"https://cdn.discordapp.com/stickers/{value['id']}.{ext}", "sticker")
            for key in sorted(value):
                child_pointer = f"{pointer}/{_json_pointer_escape(key)}"
                child = value[key]
                if key in {"image", "thumbnail", "video", "media", "file"} and isinstance(child, dict):
                    add(f"{child_pointer}/url", child, child.get("url"), f"{key}_media")
                    if child.get("proxy_url") and child.get("proxy_url") != child.get("url"):
                        add(f"{child_pointer}/proxy_url", child, child.get("proxy_url"), f"{key}_proxy_media")
                elif key in {"icon_url"} and isinstance(child, str):
                    add(child_pointer, value, child, "icon_media")
                elif key == "proxy_icon_url" and isinstance(child, str) and child != value.get("icon_url"):
                    add(child_pointer, value, child, "proxy_icon_media")
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
        "rendererAccounting": renderer_pointer_accounting(
            census,
            census["contentPointers"],
            source=source,
            stabilize_urls=True,
        ),
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
        "accountedUnclassifiedPointers": census["unclassifiedPointers"],
        "rendererAccounting": renderer_pointer_accounting(
            census,
            list(census["mutablePointers"]) + list(census["unclassifiedPointers"]),
        ),
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


def _merge_asset_receipts(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    semantic_keys = (
        "assetId", "jsonPointer", "kind", "displayFilename", "remoteUrl",
        "stableRemoteUrl", "sourceDeclaredSize", "contentType", "localRelativePath",
        "inScope", "scopeReason",
    )
    if any(first.get(key) != second.get(key) for key in semantic_keys):
        raise RecordConflictError("asset receipt semantic identity collision")
    completed = [row for row in (first, second) if row.get("status") == "complete"]
    if len(completed) == 2 and any(
        completed[0].get(key) != completed[1].get(key)
        for key in ("byteLength", "sha256", "declaredSize", "sizeSource")
    ):
        raise RecordConflictError("completed asset receipts disagree")
    if completed:
        return dict(completed[0])
    sized = [
        row for row in (first, second)
        if isinstance(row.get("declaredSize"), int) and row.get("declaredSize") >= 0
    ]
    if len(sized) == 2 and sized[0].get("declaredSize") != sized[1].get("declaredSize"):
        raise RecordConflictError("asset metadata size receipts disagree")
    if sized:
        return dict(sized[0])
    # Preserve an explicit failure over a pending placeholder.
    return dict(second if second.get("status") == "error" else first)


def _dedupe_observations(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = str(row.get("observationId") or "")
        if not identity:
            raise RichArchiveError("missing canonical identity: observationId")
        value = dict(row)
        previous = output.get(identity)
        if previous is None:
            output[identity] = value
            continue
        if any(
            previous.get(key) != value.get(key)
            for key in set(previous) | set(value)
            if key != "assetInventory"
        ):
            raise RecordConflictError(f"canonical identity collision: observationId={identity}")
        first_assets = {str(asset.get("assetId") or ""): asset for asset in previous.get("assetInventory") or []}
        second_assets = {str(asset.get("assetId") or ""): asset for asset in value.get("assetInventory") or []}
        if not all(first_assets) or set(first_assets) != set(second_assets):
            raise RecordConflictError("observation asset identity set collision")
        value["assetInventory"] = [
            _merge_asset_receipts(first_assets[key], second_assets[key])
            for key in sorted(first_assets)
        ]
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
    _reject_equal_timestamp_conflicts(
        revisions, timestamp_key="versionTimestamp", identity_key="revisionId",
    )
    observations = _dedupe_observations(
        list(existing.get("observations") or []) + list(incoming.get("observations") or []),
    )
    observations.sort(key=_observation_sort_key)
    _reject_equal_timestamp_conflicts(
        observations, timestamp_key="observedAt", identity_key="observationId",
    )
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
    errors = sorted({
        f"{asset.get('assetId')}:{asset.get('error')}"
        for observation_row in observations
        for asset in observation_row.get("assetInventory") or []
        if asset.get("status") == "error" and asset.get("error")
    })
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
    created_timestamp = _iso_timestamp(
        record.get("createdTimestamp"), field="createdTimestamp", required=True,
    )
    revisions_raw = list(record.get("contentRevisions") or [])
    observations_raw = list(record.get("observations") or [])
    if not revisions_raw or not observations_raw:
        raise RichArchiveError("canonical record has no revision or observation")
    revision_ids = [str(row.get("revisionId") or "") for row in revisions_raw]
    observation_ids = [str(row.get("observationId") or "") for row in observations_raw]
    if len(revision_ids) != len(set(revision_ids)) or len(observation_ids) != len(set(observation_ids)):
        raise RichArchiveError("duplicate revision or observation identity")
    _reject_equal_timestamp_conflicts(
        revisions_raw, timestamp_key="versionTimestamp", identity_key="revisionId",
    )
    _reject_equal_timestamp_conflicts(
        observations_raw, timestamp_key="observedAt", identity_key="observationId",
    )
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
        source_created = _iso_timestamp(source.get("timestamp"), field="timestamp", required=True)
        source_edited = _iso_timestamp(source.get("edited_timestamp"), field="edited_timestamp")
        if source_created != created_timestamp:
            raise RichArchiveError("canonical created timestamp does not match preserved source")
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
        if version_timestamp != (source_edited or source_created):
            raise RichArchiveError("revision timestamp does not match preserved source")
        if _iso_timestamp(revision.get("editedTimestamp"), field="editedTimestamp") != source_edited:
            raise RichArchiveError("revision edited timestamp does not match preserved source")
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
        if sorted(observation.get("accountedUnclassifiedPointers") or []) != census["unclassifiedPointers"]:
            raise SourceCensusError("unclassified source fields are not fully accounted for")
        expected_revision_accounting = renderer_pointer_accounting(
            census,
            census["contentPointers"],
            source=source,
            stabilize_urls=True,
        )
        expected_observation_accounting = renderer_pointer_accounting(
            census,
            list(census["mutablePointers"]) + list(census["unclassifiedPointers"]),
        )
        if revision.get("rendererAccounting") != expected_revision_accounting:
            raise SourceCensusError("content renderer accounting mismatch")
        if observation.get("rendererAccounting") != expected_observation_accounting:
            raise SourceCensusError("observation renderer accounting mismatch")
        if (
            set(census["contentPointers"])
            | set(census["mutablePointers"])
            | set(census["unclassifiedPointers"])
        ) != set(census["visiblePointers"]):
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
    expected_active_revision = max(revisions_raw, key=_revision_sort_key)
    if revision.get("revisionId") != expected_active_revision.get("revisionId"):
        raise RichArchiveError("active revision is not the latest valid revision")
    compatible = [
        row for row in observations_raw
        if row.get("revisionId") == revision.get("revisionId")
    ]
    if not compatible:
        raise RichArchiveError("active revision has no compatible observation")
    expected_active_observation = max(compatible, key=_observation_sort_key)
    if observation.get("observationId") != expected_active_observation.get("observationId"):
        raise RichArchiveError("active observation is not the latest valid observation")
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
    if record.get("authorDisplay") != _author_display(observation["apiSourcePayload"]):
        raise RichArchiveError("active author display does not match preserved source")
    return {
        "ok": not unknown_fields and not attachment_errors,
        "unknownVisibleFields": sorted(unknown_fields),
        "attachmentErrors": sorted(attachment_errors),
        "visiblePayloadSha256": expected_visible,
        "assetCount": asset_count,
    }


_MARKDOWN_ACTIVE_CHARACTERS = frozenset(r"\\`*_{}[]()<>#+-.!|:@")


def _markdown_inert(value: Any, *, inline: bool = False) -> str:
    """Encode untrusted text so it cannot become Markdown/HTML/link syntax."""
    text = unicodedata.normalize("NFC", str(value))
    if inline:
        text = " ".join(text.splitlines())
    output: list[str] = []
    for character in text:
        if ord(character) < 32 and character not in {"\n", "\t"}:
            output.append("�")
        elif character in _MARKDOWN_ACTIVE_CHARACTERS or character == "&":
            output.append(f"&#{ord(character)};")
        else:
            output.append(character)
    return "".join(output)


def _json_for_markdown(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _section(label: str, value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    # Four-space indented code has no fence delimiter that source JSON can
    # terminate.  Every line remains searchable but inert.
    indented = "\n".join(f"    {line}" for line in _json_for_markdown(value).splitlines())
    return f"\n[{label}]\n\n{indented}\n"


def render_message(record: Mapping[str, Any]) -> str:
    validate_record(record, require_assets=False)
    revision, observation = _active_parts(record)
    content = revision["content"]
    mutable = observation["mutableState"]
    source = observation["apiSourcePayload"]
    timestamp = datetime.fromisoformat(str(record["createdTimestamp"]).replace("Z", "+00:00"))
    local_time = timestamp.astimezone(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S %z")
    author = _markdown_inert(record.get("authorDisplay") or "unknown", inline=True)
    message_id = str(record["messageId"])
    marker = f"<!-- openclaw-rich-message id={message_id} visible={record['visiblePayloadSha256']} -->"
    accounting = sorted(
        list(revision.get("rendererAccounting") or [])
        + list(observation.get("rendererAccounting") or []),
        key=lambda row: (str(row.get("pointer")), str(row.get("rendererSection"))),
    )
    accounting_marker = (
        f"<!-- openclaw-rich-render-accounting pointers={len(accounting)} "
        f"sha256={json_sha256(accounting)} -->"
    )
    header = f"### {local_time} — {author} — id:{message_id}"
    sections = ""
    text = content.get("content")
    if isinstance(text, str) and text:
        safe_lines = [f"> {_markdown_inert(line)}" if line else ">" for line in text.splitlines() or [""]]
        sections += "\n[文字內容]\n\n" + "\n".join(safe_lines) + "\n"
    sections += _section("元件內容", content.get("components"))
    sections += _section("Embed", mutable.get("embeds"))
    poll = {"content": content.get("poll"), "results": mutable.get("poll")}
    if poll["content"] is not None or poll["results"] is not None:
        sections += _section("投票", poll)
    stickers = {
        "sticker_items": content.get("sticker_items"),
        "stickers": content.get("stickers"),
    }
    if any(value not in (None, [], {}) for value in stickers.values()):
        sections += _section("貼圖", stickers)
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
        "member": content.get("member"),
        "mentions": content.get("mentions"),
        "mention_roles": content.get("mention_roles"),
        "mention_everyone": content.get("mention_everyone"),
        "mention_channels": content.get("mention_channels"),
    }
    if any(value not in (None, False, [], {}) for value in identity_context.values()):
        interaction["identityContext"] = identity_context
    for key in ("activity", "application", "application_id", "thread", "resolved"):
        if content.get(key) is not None:
            interaction[key] = content.get(key)
    sections += _section("互動／系統資訊", interaction)
    status = {
        "type": mutable.get("type"),
        "editedTimestamp": mutable.get("edited_timestamp"),
        "pinned": mutable.get("pinned"),
        "tts": mutable.get("tts"),
        "flags": mutable.get("flags"),
        "reactions": mutable.get("reactions"),
        "contentRevisionCount": len(record.get("contentRevisions") or []),
        "observationCount": len(record.get("observations") or []),
    }
    sections += _section("反應／編輯狀態", status)
    unclassified_roots = {
        key: value for key, value in source.items()
        if key not in ROOT_POLICIES
    }
    sections += _section("未分類可見資料", unclassified_roots)
    if not sections:
        census = record.get("sourceCensus") or {}
        if census.get("visiblePointers") or census.get("mutablePointers") or census.get("unknownVisibleFields"):
            raise SourceCensusError("renderer produced empty output for non-empty source census")
        sections = "\n(無文字內容)\n"
    if len(record.get("contentRevisions") or []) > 1:
        history = [
            {
                "revisionId": row["revisionId"],
                "versionTimestamp": row["versionTimestamp"],
                "editedTimestamp": row.get("editedTimestamp"),
                "sourcePayloadSha256": row["sourcePayloadSha256"],
                "visibleContentSha256": row["visibleContentSha256"],
                "content": row["content"],
                "rendererAccounting": row.get("rendererAccounting"),
            }
            for row in sorted(record["contentRevisions"], key=_revision_sort_key)[:-1]
        ]
        sections += _section("歷史內容版本", history)
    if len(record.get("observations") or []) > 1:
        observation_history = [
            {
                "observationId": row["observationId"],
                "revisionId": row["revisionId"],
                "observedAt": row["observedAt"],
                "apiSourcePayloadSha256": row["apiSourcePayloadSha256"],
                "apiSourcePayload": row["apiSourcePayload"],
                "mutableStateSha256": row["mutableStateSha256"],
                "mutableState": row["mutableState"],
                "assetInventory": row.get("assetInventory"),
                "rendererAccounting": row.get("rendererAccounting"),
            }
            for row in sorted(record["observations"], key=_observation_sort_key)
            if row.get("observationId") != record.get("activeObservationId")
        ]
        sections += _section("歷史觀測版本", observation_history)
    return f"\n{marker}\n{accounting_marker}\n{header}\n{sections}"


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
        ("貼圖", bool(source.get("sticker_items")) or bool(source.get("stickers"))),
        ("轉寄快照", source.get("message_snapshots")),
        ("附件", source.get("attachments") or observation.get("assetInventory")),
        ("回覆關係", source.get("message_reference") or source.get("referenced_message")),
        ("互動／系統資訊", bool(source.get("author")) or bool(source.get("member")) or bool(source.get("mentions"))
         or bool(source.get("mention_roles")) or bool(source.get("mention_everyone"))
         or bool(source.get("mention_channels"))
         or any(source.get(key) is not None for key in (
             "interaction", "interaction_metadata", "role_subscription_data", "call", "purchase_notification",
             "activity", "application", "application_id", "thread", "resolved",
         ))),
    )
    required.extend(label for label, present in mappings if present)
    # Type/edit/pin/TTS/flags/reactions and version counters are always made
    # explicit, including otherwise-empty system messages.
    required.append("反應／編輯狀態")
    if len(record.get("contentRevisions") or []) > 1:
        required.append("歷史內容版本")
    if len(record.get("observations") or []) > 1:
        required.append("歷史觀測版本")
    if any(key not in ROOT_POLICIES for key in source):
        required.append("未分類可見資料")
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
    path = _lexical_absolute(path)
    # Check the caller-supplied path graph before any mkdir/tempfile mutation.
    reject_symlink_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    reject_symlink_path(path)
    if os.path.lexists(path) and not _regular_single_link(path):
        raise RichArchiveError(f"managed target must be a single-link regular file: {path}")
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


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute lexical path without resolving symlink components."""
    return Path(os.path.abspath(os.fspath(path)))


def reject_symlink_path(path: Path) -> None:
    absolute = _lexical_absolute(path)
    current = Path(absolute.parts[0])
    for part in absolute.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RichArchiveError("symlinked managed path is forbidden")


def contained_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise RichArchiveError("invalid managed relative path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RichArchiveError(f"unsafe managed relative path: {relative}")
    root_abs = _lexical_absolute(root)
    # Never call resolve() before checking the original path graph: doing so
    # would erase evidence that a caller supplied a symlinked root/component.
    reject_symlink_path(root_abs)
    candidate = root_abs.joinpath(*pure.parts)
    reject_symlink_path(candidate)
    try:
        contained = os.path.commonpath((os.fspath(root_abs), os.fspath(candidate))) == os.fspath(root_abs)
    except ValueError:
        contained = False
    if not contained:
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,126}[A-Za-z0-9]", generation_id):
        if not re.fullmatch(r"[A-Za-z0-9]", generation_id):
            raise GenerationError("unsafe or reserved generation id")
    reserved = {
        "current", "generations", "staging", "canonical", "raw",
        "attachments", "receipts", "legacy-retained", "tmp", "temp",
    }
    if generation_id.casefold() in reserved or generation_id.startswith(".staging-"):
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


@dataclass
class AssetProbeBudget:
    """One shared unknown-size metadata budget for a batch or full run."""

    remaining_requests: int
    deadline_monotonic: float

    @classmethod
    def from_limits(cls, limits: AssetLimits) -> "AssetProbeBudget":
        return cls(
            remaining_requests=limits.max_unknown_size_probes,
            deadline_monotonic=time.monotonic() + limits.metadata_probe_elapsed_seconds,
        )

    def ensure_capacity(self, count: int) -> None:
        if count > self.remaining_requests:
            raise AssetDownloadError("unknown-size asset metadata probe quota exceeded")
        if time.monotonic() > self.deadline_monotonic:
            raise AssetDownloadError("asset metadata probe elapsed-time cap exceeded")

    def consume(self) -> None:
        self.ensure_capacity(1)
        self.remaining_requests -= 1


@dataclass
class AssetRunBudget:
    """Cumulative file/byte/probe budget shared across every rebuild entry."""

    limits: AssetLimits
    probe_budget: AssetProbeBudget
    file_count: int = 0
    declared_bytes: int = 0

    @classmethod
    def from_limits(cls, limits: AssetLimits) -> "AssetRunBudget":
        return cls(limits=limits, probe_budget=AssetProbeBudget.from_limits(limits))

    def preflight_entry(
        self,
        assets: Sequence[Mapping[str, Any]],
        destination_root: Path,
        *,
        assume_unknown_max: bool,
    ) -> dict[str, int]:
        return preflight_asset_capacity(
            assets,
            destination_root,
            limits=self.limits,
            run_file_count=self.file_count,
            run_declared_bytes=self.declared_bytes,
            assume_unknown_max=assume_unknown_max,
        )

    def commit_entry(self, result: Mapping[str, Any]) -> None:
        files = result.get("files")
        declared = result.get("declaredBytes")
        if not isinstance(files, int) or not isinstance(declared, int):
            raise AssetDownloadError("asset run budget cannot commit invalid preflight totals")
        self.file_count += files
        self.declared_bytes += declared
        if self.file_count > self.limits.full_run_files or self.declared_bytes > self.limits.full_run_bytes:
            raise AssetDownloadError("full-run asset quota exceeded")


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
    probe = _lexical_absolute(destination_root)
    reject_symlink_path(probe)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    reject_symlink_path(probe)
    if not probe.exists() or not probe.is_dir():
        raise AssetDownloadError("no existing directory is available for disk capacity preflight")
    free = shutil.disk_usage(probe).free
    required = declared + limits.disk_reserve_bytes
    if free < required:
        raise AssetDownloadError("insufficient disk capacity before attachment download")
    return {"files": len(in_scope), "declaredBytes": declared, "freeBytes": free, "requiredBytes": required}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _close_response_safely(response: Any) -> None:
    try:
        response.close()
    except Exception:
        # Cleanup must never mask the security or integrity error that caused
        # the response to be rejected (including synthetic HTTPError objects).
        pass


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
                    _close_response_safely(exc)
                    raise AssetDownloadError(f"asset metadata HTTP status {exc.code}") from exc
                location = exc.headers.get("Location")
                if not location or redirects >= self.limits.max_redirects:
                    _close_response_safely(exc)
                    raise AssetDownloadError("asset metadata redirect limit exceeded") from exc
                try:
                    url, _ = self._validated_url(urllib.parse.urljoin(url, location), expected_host=original_host)
                finally:
                    _close_response_safely(exc)
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
                _close_response_safely(response)

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
        if os.path.lexists(target) and not _regular_single_link(target):
            raise AssetDownloadError("existing asset target is not a single-link regular file")
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
                    _close_response_safely(exc)
                    raise AssetDownloadError(f"asset HTTP status {exc.code}") from exc
                location = exc.headers.get("Location")
                if not location or redirects >= self.limits.max_redirects:
                    _close_response_safely(exc)
                    raise AssetDownloadError("asset redirect limit exceeded") from exc
                try:
                    url, _ = self._validated_url(urllib.parse.urljoin(url, location), expected_host=original_host)
                finally:
                    _close_response_safely(exc)
                redirects += 1
                continue
            status = getattr(response, "status", response.getcode())
            if status in REDIRECT_CODES:
                location = response.headers.get("Location")
                _close_response_safely(response)
                if not location or redirects >= self.limits.max_redirects:
                    raise AssetDownloadError("asset redirect limit exceeded")
                url, _ = self._validated_url(urllib.parse.urljoin(url, location), expected_host=original_host)
                redirects += 1
                continue
            if status != 200:
                _close_response_safely(response)
                raise AssetDownloadError(f"asset HTTP status {status}")
            encoding = (response.headers.get("Content-Encoding") or "identity").lower()
            if encoding != "identity":
                _close_response_safely(response)
                raise AssetDownloadError("compressed asset response is forbidden")
            content_length = response.headers.get("Content-Length")
            if content_length is None or not content_length.isdigit() or int(content_length) != expected_size:
                _close_response_safely(response)
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
                _close_response_safely(response)
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            row.update({"status": "complete", "byteLength": count, "sha256": digest.hexdigest(), "error": None})
            return row


def resolve_asset_sizes(
    record: Mapping[str, Any],
    downloader: AssetDownloader,
    *,
    probe_budget: AssetProbeBudget | None = None,
) -> dict[str, Any]:
    """Fill missing in-scope sizes from safe HEAD metadata before mutation."""
    updated = dict(record)
    observations = [dict(row) for row in updated.get("observations") or []]
    unknown_count = sum(
        1 for observation in observations for asset in observation.get("assetInventory") or []
        if asset.get("inScope") and asset.get("declaredSize") is None
    )
    budget = probe_budget or AssetProbeBudget.from_limits(downloader.limits)
    budget.ensure_capacity(unknown_count)
    for observation in observations:
        output: list[dict[str, Any]] = []
        for asset in observation.get("assetInventory") or []:
            row = dict(asset)
            if row.get("inScope") and row.get("declaredSize") is None:
                budget.consume()
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
    root = _lexical_absolute(root)
    reject_symlink_path(root)
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


def _validated_entry_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise GenerationError("live evidence entry identity is missing")
    channel_id = str(value.get("channelId") or "")
    relative = str(value.get("relativePath") or "")
    if not channel_id.isdigit() or not relative or "\\" in relative:
        raise GenerationError("live evidence entry identity is invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise GenerationError("live evidence entry path is unsafe")
    normalized = unicodedata.normalize("NFKC", relative).casefold()
    if value.get("normalizedRelativePath") != normalized:
        raise GenerationError("live evidence normalized entry identity mismatch")
    return {
        "channelId": channel_id,
        "relativePath": relative,
        "normalizedRelativePath": normalized,
    }


def _asset_source_binding(asset: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: asset.get(key)
        for key in (
            "assetId", "jsonPointer", "kind", "displayFilename", "remoteUrl",
            "stableRemoteUrl", "sourceDeclaredSize", "contentType",
            "localRelativePath", "inScope", "scopeReason",
        )
    }


def _active_live_binding(record: Mapping[str, Any]) -> dict[str, Any]:
    revision, observation = _active_parts(record)
    assets = sorted(
        (_asset_source_binding(asset) for asset in observation.get("assetInventory") or []),
        key=lambda row: (str(row.get("jsonPointer")), str(row.get("assetId"))),
    )
    accounting = sorted(
        list(revision.get("rendererAccounting") or [])
        + list(observation.get("rendererAccounting") or []),
        key=lambda row: (str(row.get("pointer")), str(row.get("rendererSection"))),
    )
    return {
        "messageId": str(record["messageId"]),
        "channelId": str(record["channelId"]),
        "apiSourcePayloadSha256": str(observation["apiSourcePayloadSha256"]),
        "sourcePayloadSha256": str(revision["sourcePayloadSha256"]),
        "visiblePayloadSha256": str(record["visiblePayloadSha256"]),
        "sourceCensusSha256": str(record["sourceCensus"]["censusSha256"]),
        "rendererAccountingSha256": json_sha256(accounting),
        "visiblePointerCount": len(record["sourceCensus"]["visiblePointers"]),
        "assets": assets,
        "assetInventorySha256": json_sha256(assets),
    }


def _validate_immutable_evidence_reference(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GenerationError("immutable pre-repair evidence reference is missing")
    required_hashes = (
        "archiveTreeManifestSha256", "stateSha256", "queueSha256", "verificationSha256",
    )
    snapshot_id = str(value.get("snapshotId") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", snapshot_id):
        raise GenerationError("immutable evidence snapshot identity is invalid")
    if value.get("status") != "PASS" or any(
        not re.fullmatch(r"[0-9a-f]{64}", str(value.get(key) or ""))
        for key in required_hashes
    ):
        raise GenerationError("immutable evidence cryptographic reference is invalid")
    verified_at = _iso_timestamp(value.get("verifiedAt"), field="immutableEvidence.verifiedAt", required=True)
    return {
        "schemaVersion": "openclaw-discord-immutable-evidence-ref.v1",
        "snapshotId": snapshot_id,
        "status": "PASS",
        "verifiedAt": verified_at,
        **{key: str(value[key]) for key in required_hashes},
    }


def build_live_inventory_evidence(
    messages: Sequence[Mapping[str, Any]],
    *,
    channel_id: str,
    relative_path: str,
    inventory_digest: str,
    inventory_observed_at: str,
    verified_cutoff: str | None,
    fetch_started_at: str,
    fetch_completed_at: str,
    page_count: int,
    immutable_evidence: Mapping[str, Any],
    allowed_cdn_hosts: frozenset[str] = DEFAULT_CDN_HOSTS,
) -> dict[str, Any]:
    """Create concrete, self-checking live evidence from a completed API fetch."""
    identity = _validated_entry_identity({
        "channelId": str(channel_id),
        "relativePath": relative_path,
        "normalizedRelativePath": unicodedata.normalize("NFKC", relative_path).casefold(),
    })
    if not re.fullmatch(r"[0-9a-f]{64}", inventory_digest):
        raise GenerationError("fresh Discord inventory digest is invalid")
    observed_inventory = _iso_timestamp(
        inventory_observed_at, field="inventoryObservedAt", required=True,
    )
    started = _iso_timestamp(fetch_started_at, field="fetchStartedAt", required=True)
    completed = _iso_timestamp(fetch_completed_at, field="fetchCompletedAt", required=True)
    if started > completed:
        raise GenerationError("live fetch completion precedes start")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise GenerationError("completed live fetch requires at least one page observation")
    normalized = [
        normalize_message(
            message,
            expected_channel_id=channel_id,
            observed_at=completed,
            allowed_cdn_hosts=allowed_cdn_hosts,
        )
        for message in messages
    ]
    rows = sorted((_active_live_binding(record) for record in normalized), key=lambda row: int(row["messageId"]))
    ids = [row["messageId"] for row in rows]
    if len(ids) != len(set(ids)):
        raise GenerationError("live evidence contains duplicate message IDs")
    if rows:
        if not isinstance(verified_cutoff, str) or not verified_cutoff.isdigit():
            raise GenerationError("non-empty live evidence requires a numeric cutoff")
        if max(ids, key=int) != verified_cutoff or any(int(value) > int(verified_cutoff) for value in ids):
            raise GenerationError("live evidence IDs do not terminate at the verified cutoff")
    elif verified_cutoff not in (None, ""):
        raise GenerationError("truly empty live evidence must have a null cutoff")
    immutable = _validate_immutable_evidence_reference(immutable_evidence)
    body: dict[str, Any] = {
        "schemaVersion": LIVE_EVIDENCE_SCHEMA,
        "entryIdentity": identity,
        "inventory": {
            "complete": True,
            "digest": inventory_digest,
            "observedAt": observed_inventory,
            "channelId": str(channel_id),
        },
        "verifiedCutoff": verified_cutoff or None,
        "trulyEmpty": not rows,
        "enumeration": {
            "source": "discord-api-direct",
            "complete": True,
            "terminalPageObserved": True,
            "pageCount": page_count,
            "fetchedMessageCount": len(rows),
            "fetchStartedAt": started,
            "fetchCompletedAt": completed,
            "pagePayloadSha256": json_sha256(rows),
        },
        "messages": rows,
        "immutableEvidence": immutable,
    }
    body["evidenceSha256"] = json_sha256(body)
    return body


def _load_generation_records(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], int]:
    records: dict[str, dict[str, Any]] = {}
    by_day: dict[str, list[dict[str, Any]]] = {}
    duplicates = 0
    canonical_root = root / "canonical"
    if canonical_root.exists():
        reject_symlink_path(canonical_root)
    paths = sorted(canonical_root.glob("*.jsonl")) if canonical_root.is_dir() else []
    for path in paths:
        rows = load_jsonl(path)
        by_day[path.stem] = rows
        for row in rows:
            message_id = str(row.get("messageId") or "")
            if message_id in records:
                duplicates += 1
            else:
                records[message_id] = row
    return records, by_day, duplicates


def _generation_projection(root: Path) -> dict[str, Any]:
    records, by_day, duplicate_ids = _load_generation_records(root)
    unknown = 0
    attachment_errors = 0
    section_errors = 0
    markdown_errors = 0
    markdown_verified = 0
    binary_expected = 0
    binary_verified = 0
    visible_pointers = 0
    for day, rows in by_day.items():
        raw_path = root / "raw" / f"{day}.md"
        expected_bytes = render_day(rows).encode("utf-8")
        actual_bytes = raw_path.read_bytes() if _regular_single_link(raw_path) else b""
        markers = parse_markdown_markers(actual_bytes.decode("utf-8") if actual_bytes else "")
        expected_markers = [(str(row["messageId"]), str(row["visiblePayloadSha256"])) for row in rows]
        if actual_bytes == expected_bytes and markers == expected_markers:
            markdown_verified += len(rows)
        else:
            markdown_errors += 1
        for row in rows:
            outcome = validate_record(row, generation_root=root)
            unknown += len(outcome["unknownVisibleFields"])
            attachment_errors += len(outcome["attachmentErrors"])
            visible_pointers += len(row["sourceCensus"]["visiblePointers"])
            for label in required_render_sections(row):
                if f"\n[{label}]\n" not in render_message(row):
                    section_errors += 1
            for observation in row.get("observations") or []:
                for asset in observation.get("assetInventory") or []:
                    if not asset.get("inScope"):
                        continue
                    binary_expected += 1
                    path = contained_path(root, str(asset.get("localRelativePath") or ""))
                    if (
                        asset.get("status") == "complete"
                        and isinstance(asset.get("byteLength"), int)
                        and re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256") or ""))
                        and _regular_single_link(path)
                        and path.stat().st_size == asset["byteLength"]
                        and file_sha256(path) == asset["sha256"]
                    ):
                        binary_verified += 1
    raw_root = root / "raw"
    raw_days = {
        path.stem for path in raw_root.glob("*.md") if _regular_single_link(path)
    } if raw_root.is_dir() else set()
    if raw_days != set(by_day):
        markdown_errors += 1
    return {
        "records": records,
        "byDay": by_day,
        "duplicateCanonicalIds": duplicate_ids,
        "unknownVisibleFields": unknown,
        "attachmentErrors": attachment_errors,
        "sectionCoverageErrors": section_errors,
        "markdownErrors": markdown_errors,
        "markdownVerified": markdown_verified,
        "binaryExpected": binary_expected,
        "binaryVerified": binary_verified,
        "visiblePointerCount": visible_pointers,
    }


def _validated_live_evidence(root: Path, evidence: Any, projection: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(evidence, Mapping) or evidence.get("schemaVersion") != LIVE_EVIDENCE_SCHEMA:
        raise GenerationError("full PASS live evidence schema is missing or unsupported")
    body = dict(evidence)
    supplied_digest = body.pop("evidenceSha256", None)
    if supplied_digest != json_sha256(body):
        raise GenerationError("live evidence checksum mismatch")
    identity = _validated_entry_identity(evidence.get("entryIdentity"))
    inventory = evidence.get("inventory")
    if not isinstance(inventory, Mapping) or inventory.get("complete") is not True:
        errors.append("inventory_not_complete")
        inventory = {}
    if (
        not re.fullmatch(r"[0-9a-f]{64}", str(inventory.get("digest") or ""))
        or str(inventory.get("channelId") or "") != identity["channelId"]
    ):
        errors.append("inventory_identity_or_digest_invalid")
    _iso_timestamp(inventory.get("observedAt"), field="inventory.observedAt", required=True)
    immutable = _validate_immutable_evidence_reference(evidence.get("immutableEvidence"))
    enumeration = evidence.get("enumeration")
    if not isinstance(enumeration, Mapping):
        raise GenerationError("live enumeration evidence is missing")
    messages = evidence.get("messages")
    if not isinstance(messages, list) or any(not isinstance(row, Mapping) for row in messages):
        raise GenerationError("live evidence messages are invalid")
    message_rows = [dict(row) for row in messages]
    if enumeration.get("source") != "discord-api-direct" or enumeration.get("complete") is not True or enumeration.get("terminalPageObserved") is not True:
        errors.append("live_enumeration_incomplete")
    if not isinstance(enumeration.get("pageCount"), int) or isinstance(enumeration.get("pageCount"), bool) or enumeration.get("pageCount") < 1:
        errors.append("live_page_proof_missing")
    if enumeration.get("fetchedMessageCount") != len(message_rows) or enumeration.get("pagePayloadSha256") != json_sha256(message_rows):
        errors.append("live_page_denominator_mismatch")
    started = _iso_timestamp(enumeration.get("fetchStartedAt"), field="fetchStartedAt", required=True)
    completed = _iso_timestamp(enumeration.get("fetchCompletedAt"), field="fetchCompletedAt", required=True)
    if started > completed:
        errors.append("live_fetch_time_invalid")
    ids = [str(row.get("messageId") or "") for row in message_rows]
    if any(not value.isdigit() for value in ids) or len(ids) != len(set(ids)) or ids != sorted(ids, key=int):
        errors.append("live_message_identity_set_invalid")
    records = projection["records"]
    if set(ids) != set(records):
        errors.append("live_and_canonical_id_sets_differ")
    cutoff = evidence.get("verifiedCutoff")
    truly_empty = evidence.get("trulyEmpty")
    if ids:
        if truly_empty is not False or not isinstance(cutoff, str) or not cutoff.isdigit() or max(ids, key=int) != cutoff:
            errors.append("verified_cutoff_mismatch")
    elif not (
        truly_empty is True and cutoff is None and enumeration.get("terminalPageObserved") is True
        and enumeration.get("fetchedMessageCount") == 0 and enumeration.get("pageCount", 0) >= 1
    ):
        errors.append("empty_inventory_not_independently_evidenced")
    matched = 0
    for row in message_rows:
        message_id = str(row.get("messageId") or "")
        record = records.get(message_id)
        if record is None:
            continue
        expected = _active_live_binding(record)
        if row == expected and row.get("channelId") == identity["channelId"]:
            matched += 1
        else:
            errors.append(f"live_message_binding_mismatch:{message_id}")
    channel_ids = {str(record.get("channelId") or "") for record in records.values()}
    if channel_ids and channel_ids != {identity["channelId"]}:
        errors.append("canonical_entry_channel_identity_mismatch")
    counts = {
        "inventory": {"expected": 1, "verified": 1 if not any(error.startswith("inventory_") for error in errors) else 0},
        "messages": {"expected": len(message_rows), "verified": matched},
        "visiblePointers": {
            "expected": sum(int(row.get("visiblePointerCount") or 0) for row in message_rows),
            "verified": projection["visiblePointerCount"] if matched == len(message_rows) else 0,
        },
        "markdownMessages": {"expected": len(message_rows), "verified": projection["markdownVerified"]},
        "binaryAssets": {"expected": projection["binaryExpected"], "verified": projection["binaryVerified"]},
    }
    return {
        "identity": identity,
        "inventory": dict(inventory),
        "immutable": immutable,
        "cutoff": cutoff,
        "counts": counts,
        "evidenceSha256": str(supplied_digest),
    }, sorted(set(errors))


def _coverage_percent(row: Mapping[str, Any]) -> int:
    expected = row.get("expected")
    verified = row.get("verified")
    if not isinstance(expected, int) or not isinstance(verified, int) or expected < 0 or verified < 0:
        raise GenerationError("coverage count is invalid")
    if expected == 0:
        return 100 if verified == 0 else 0
    return 100 if verified == expected else int((verified * 100) // expected)


def build_full_pass_receipt(root: Path, evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute a v2 receipt from concrete live evidence and staged bytes."""
    projection = _generation_projection(root)
    validated, evidence_errors = _validated_live_evidence(root, evidence, projection)
    counts = validated["counts"]
    inventory = generation_inventory(root)
    errors = list(evidence_errors)
    if projection["duplicateCanonicalIds"]:
        errors.append("duplicate_canonical_ids")
    if projection["unknownVisibleFields"]:
        errors.append("unknown_visible_fields")
    if projection["attachmentErrors"]:
        errors.append("attachment_errors")
    if projection["markdownErrors"] or projection["sectionCoverageErrors"]:
        errors.append("markdown_projection_errors")
    coverage = {
        "inventoryCoverage": _coverage_percent(counts["inventory"]),
        "idCoverage": _coverage_percent(counts["messages"]),
        "visibleTextCoverage": _coverage_percent(counts["visiblePointers"]),
        "markdownCoverage": _coverage_percent(counts["markdownMessages"]),
        "binaryAssetCoverage": _coverage_percent(counts["binaryAssets"]),
    }
    if any(value != 100 for value in coverage.values()):
        errors.append("coverage_not_complete")
    binding = {
        "entryIdentity": validated["identity"],
        "inventoryDigest": validated["inventory"].get("digest"),
        "verifiedCutoff": validated["cutoff"],
        "liveEvidenceSha256": validated["evidenceSha256"],
        "immutableEvidenceSha256": json_sha256(validated["immutable"]),
        "coverageCounts": counts,
        "contentGenerationSha256": inventory["contentGenerationSha256"],
    }
    receipt: dict[str, Any] = {
        "schemaVersion": ENTRY_RECEIPT_SCHEMA,
        "gateStatus": "PASS" if not errors else "FAIL",
        **coverage,
        "coverageCounts": counts,
        "liveErrors": len(evidence_errors),
        "duplicateCanonicalIds": projection["duplicateCanonicalIds"],
        "unknownVisibleFields": projection["unknownVisibleFields"],
        "attachmentErrors": projection["attachmentErrors"],
        "inventoryComplete": validated["inventory"].get("complete") is True,
        "inventoryDigest": validated["inventory"].get("digest"),
        "verifiedCutoff": validated["cutoff"],
        "entryIdentity": validated["identity"],
        "entryIdentitySha256": json_sha256(validated["identity"]),
        "liveEvidencePath": "receipts/live-inventory-evidence.json",
        "liveEvidenceSha256": validated["evidenceSha256"],
        "immutableEvidenceVerified": "PASS",
        "immutableEvidenceSha256": json_sha256(validated["immutable"]),
        "contentGenerationSha256": inventory["contentGenerationSha256"],
        "fullGateBindingSha256": json_sha256(binding),
        "errors": sorted(set(errors)),
    }
    return receipt


def verify_generation(root: Path, *, require_full_gate: bool = False) -> dict[str, Any]:
    root = _lexical_absolute(root)
    reject_symlink_path(root)
    manifest_path = root / "generation-manifest.json"
    if not _regular_single_link(manifest_path):
        raise GenerationError("generation manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = generation_inventory(root)
    if manifest != actual:
        raise GenerationError("generation manifest mismatch")
    projection = _generation_projection(root)
    receipt_path = root / "receipts" / "rich-archive-latest.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if _regular_single_link(receipt_path) else {}
    evidence_path = root / "receipts" / "live-inventory-evidence.json"
    full_gate_present = (
        receipt.get("schemaVersion") == ENTRY_RECEIPT_SCHEMA
        and receipt.get("liveEvidencePath") == "receipts/live-inventory-evidence.json"
        and _regular_single_link(evidence_path)
    )
    full_gate_errors: list[str] = []
    expected_receipt: dict[str, Any] | None = None
    if full_gate_present:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        try:
            expected_receipt = build_full_pass_receipt(root, evidence)
        except RichArchiveError as exc:
            full_gate_errors.append(f"live_evidence_invalid:{type(exc).__name__}")
        else:
            full_gate_errors.extend(expected_receipt.get("errors") or [])
            if receipt != expected_receipt:
                full_gate_errors.append("receipt_does_not_equal_recomputed_full_gate")
            if expected_receipt.get("gateStatus") != "PASS":
                full_gate_errors.append("recomputed_full_gate_not_pass")
    else:
        full_gate_errors.append("concrete_live_evidence_or_v2_receipt_missing")
    if receipt.get("gateStatus") == "PASS" and full_gate_errors:
        raise GenerationError("PASS receipt failed exact full live completeness validation")
    if require_full_gate and (
        not full_gate_present or receipt.get("gateStatus") != "PASS" or full_gate_errors
    ):
        raise GenerationError("full live completeness gate is absent or not PASS")
    ok = (
        projection["duplicateCanonicalIds"] == 0
        and projection["unknownVisibleFields"] == 0
        and projection["attachmentErrors"] == 0
        and projection["markdownErrors"] == 0
        and projection["sectionCoverageErrors"] == 0
    )
    return {
        "ok": ok,
        "verified": ok,
        "gateStatus": receipt.get("gateStatus", "INCOMPLETE"),
        "fullGatePresent": full_gate_present,
        "fullGateErrors": full_gate_errors,
        "records": len(projection["records"]),
        "duplicateCanonicalIds": projection["duplicateCanonicalIds"],
        "unknownVisibleFields": projection["unknownVisibleFields"],
        "attachmentErrors": projection["attachmentErrors"],
        "markdownErrors": projection["markdownErrors"],
        "sectionCoverageErrors": projection["sectionCoverageErrors"],
        "generationSha256": actual["generationSha256"],
    }


_LOCK_TOKEN_GUARD = object()


class ArchiveLockToken:
    """Unforgeable-in-process proof that the shared backup lock is held."""

    def __init__(self, path: Path, handle: Any, *, guard: object) -> None:
        if guard is not _LOCK_TOKEN_GUARD:
            raise RichArchiveError("archive lock token cannot be constructed externally")
        info = os.fstat(handle.fileno())
        self.path = _lexical_absolute(path)
        self._handle = handle
        self._guard = guard
        self._pid = os.getpid()
        self._device = info.st_dev
        self._inode = info.st_ino
        self._nonce = secrets.token_hex(32)
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._closed = True

    def __enter__(self) -> "ArchiveLockToken":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


class RichArchiveStore:
    """Versioned per-entry archive with atomic CURRENT pointer publication."""

    def __init__(self, entry_root: Path, *, lock_path: Path | None = None) -> None:
        self.entry_root = _lexical_absolute(entry_root)
        self.generations = self.entry_root / "generations"
        self.staging = self.entry_root / ".staging"
        self.pointer_path = self.entry_root / "CURRENT.json"
        self.journal_path = self.entry_root / ".rich-archive-transaction.json"
        self.lock_path = _lexical_absolute(lock_path) if lock_path is not None else None

    def acquire_lock(self) -> ArchiveLockToken:
        if self.lock_path is None:
            raise RichArchiveError("shared backup lock path is required for archive mutation")
        reject_symlink_path(self.lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        reject_symlink_path(self.lock_path)
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
        return ArchiveLockToken(self.lock_path, handle, guard=_LOCK_TOKEN_GUARD)

    def _require_lock(self, lock_token: ArchiveLockToken | None) -> ArchiveLockToken:
        if self.lock_path is None:
            raise RichArchiveError("shared backup lock path is required for archive mutation")
        if (
            not isinstance(lock_token, ArchiveLockToken)
            or lock_token._guard is not _LOCK_TOKEN_GUARD
            or lock_token.closed
            or lock_token._pid != os.getpid()
            or lock_token.path != self.lock_path
        ):
            raise RichArchiveError("valid shared backup lock ownership token is required")
        try:
            descriptor_info = os.fstat(lock_token._handle.fileno())
            path_info = self.lock_path.lstat()
        except (OSError, ValueError) as exc:
            raise RichArchiveError("shared backup lock ownership token is no longer valid") from exc
        if (
            descriptor_info.st_dev != lock_token._device
            or descriptor_info.st_ino != lock_token._inode
            or path_info.st_dev != lock_token._device
            or path_info.st_ino != lock_token._inode
            or not stat.S_ISREG(path_info.st_mode)
            or path_info.st_nlink != 1
        ):
            raise RichArchiveError("shared backup lock ownership token no longer matches lock file")
        return lock_token

    def _pointer_body(self, generation_id: str, generation_sha256: str) -> dict[str, str]:
        return {
            "schemaVersion": POINTER_SCHEMA,
            "generationId": generation_id,
            "generationSha256": generation_sha256,
        }

    def resolve_current(self) -> Path | None:
        reject_symlink_path(self.entry_root)
        reject_symlink_path(self.pointer_path)
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

    def create_stage(
        self,
        generation_id: str,
        *,
        copy_current: bool = True,
        lock_token: ArchiveLockToken | None = None,
    ) -> Path:
        self._require_lock(lock_token)
        generation_id = _validated_generation_id(generation_id)
        reject_symlink_path(self.entry_root)
        reject_symlink_path(self.generations)
        reject_symlink_path(self.staging)
        self.entry_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.generations.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        reject_symlink_path(self.generations)
        reject_symlink_path(self.staging)
        stage = contained_path(self.staging, generation_id)
        if os.path.lexists(stage):
            if stage.is_symlink() or not stage.is_dir():
                raise GenerationError("invalid existing stage")
            return stage
        current = self.resolve_current() if copy_current else None
        if current is not None:
            verify_generation(current)
            shutil.copytree(current, stage, copy_function=shutil.copy2, symlinks=True)
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
        lock_token: ArchiveLockToken | None = None,
        run_budget: AssetRunBudget | None = None,
    ) -> dict[str, Any]:
        owned_lock = None
        if lock_token is None:
            owned_lock = self.acquire_lock()
            lock_token = owned_lock
        self._require_lock(lock_token)
        try:
            current = self.resolve_current()
            if current is None:
                raise GenerationError("rich archive CURRENT generation is missing; full rebuild is required")
            normalized = [
                normalize_message(message, expected_channel_id=channel_id, observed_at=observed_at,
                                  allowed_cdn_hosts=(downloader.allowed_hosts if downloader else DEFAULT_CDN_HOSTS))
                for message in messages
            ]
            by_day: dict[str, list[dict[str, Any]]] = {
                path.stem: load_jsonl(path)
                for path in sorted((current / "canonical").glob("*.jsonl"))
            }
            for message, record in zip(messages, normalized):
                day = canonical_day(message)
                by_day[day] = merge_day_records(by_day.get(day, []), [record])
            if downloader is not None:
                budget = run_budget or AssetRunBudget.from_limits(downloader.limits)
                if budget.limits != downloader.limits:
                    raise AssetDownloadError("shared asset run budget limits do not match downloader limits")
                assets = [
                    asset for rows in by_day.values() for record in rows
                    for observation in record["observations"]
                    for asset in observation["assetInventory"]
                ]
                budget.preflight_entry(assets, self.entry_root, assume_unknown_max=True)
                unknown_count = sum(
                    1 for asset in assets
                    if asset.get("inScope") and asset.get("declaredSize") is None
                )
                probe_budget = budget.probe_budget
                probe_budget.ensure_capacity(unknown_count)
                by_day = {
                    day: [
                        resolve_asset_sizes(record, downloader, probe_budget=probe_budget)
                        for record in rows
                    ]
                    for day, rows in by_day.items()
                }
                assets = [
                    asset for rows in by_day.values() for record in rows
                    for observation in record["observations"]
                    for asset in observation["assetInventory"]
                ]
                exact_capacity = budget.preflight_entry(
                    assets, self.entry_root, assume_unknown_max=False,
                )
                budget.commit_entry(exact_capacity)
            stage = self.create_stage(
                generation_id, copy_current=True, lock_token=lock_token,
            )
            if downloader is not None:
                by_day = {
                    day: [apply_asset_results(record, downloader, stage) for record in rows]
                    for day, rows in by_day.items()
                }
            for day, merged in by_day.items():
                canonical_path = stage / "canonical" / f"{day}.jsonl"
                atomic_jsonl(canonical_path, merged)
                _atomic_bytes(stage / "raw" / f"{day}.md", render_day(merged).encode("utf-8"))
            (stage / "receipts" / "live-inventory-evidence.json").unlink(missing_ok=True)
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
            self.publish_stage(
                stage, generation_id, manifest["generationSha256"], lock_token=lock_token,
            )
            return {"generationId": generation_id, "verified": True, **local}
        finally:
            if owned_lock is not None:
                owned_lock.close()

    def install_full_pass_evidence(
        self,
        stage: Path,
        evidence: Mapping[str, Any],
        *,
        lock_token: ArchiveLockToken | None = None,
    ) -> dict[str, Any]:
        """Install concrete evidence and a recomputed PASS receipt on a stage."""
        self._require_lock(lock_token)
        stage = _lexical_absolute(stage)
        if self.staging not in stage.parents or stage.parent != self.staging:
            raise GenerationError("full PASS evidence may only be installed on an owned stage")
        reject_symlink_path(stage)
        if not stage.is_dir():
            raise GenerationError("full PASS evidence stage is missing")
        evidence_path = contained_path(stage, "receipts/live-inventory-evidence.json")
        atomic_json(evidence_path, evidence)
        receipt = build_full_pass_receipt(stage, evidence)
        if receipt.get("gateStatus") != "PASS":
            raise GenerationError("concrete live evidence did not satisfy the full PASS gate")
        atomic_json(stage / "receipts" / "rich-archive-latest.json", receipt)
        manifest = generation_inventory(stage)
        atomic_json(stage / "generation-manifest.json", manifest)
        verified = verify_generation(stage, require_full_gate=True)
        if not verified["ok"]:
            raise GenerationError("full PASS evidence stage failed local verification")
        return {"receipt": receipt, "manifest": manifest, "verified": verified}

    def publish_stage(
        self,
        stage: Path,
        generation_id: str,
        generation_sha256: str,
        *,
        require_full_gate: bool = False,
        lock_token: ArchiveLockToken | None = None,
    ) -> None:
        self._require_lock(lock_token)
        generation_id = _validated_generation_id(generation_id)
        if not re.fullmatch(r"[0-9a-f]{64}", generation_sha256):
            raise GenerationError("invalid generation checksum")
        final = contained_path(self.generations, generation_id)
        expected_stage = contained_path(self.staging, generation_id)
        if _lexical_absolute(stage) != expected_stage or stage.is_symlink() or not stage.is_dir():
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

    def recover_journal(self, *, lock_token: ArchiveLockToken | None = None) -> dict[str, Any]:
        self._require_lock(lock_token)
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
    "ArchiveLockToken", "AssetDownloadError", "AssetDownloader", "AssetLimits",
    "AssetProbeBudget", "AssetRunBudget", "DEFAULT_CDN_HOSTS", "ENTRY_RECEIPT_SCHEMA",
    "GENERATION_MANIFEST_SCHEMA", "GenerationError", "LIVE_EVIDENCE_SCHEMA", "RECORD_SCHEMA",
    "RichArchiveError", "RichArchiveStore", "SOURCE_CENSUS_SCHEMA",
    "SourceBoundsError", "SourceCensusError", "apply_asset_results", "atomic_json",
    "atomic_jsonl", "canonical_day", "contained_path", "file_sha256",
    "build_full_pass_receipt", "build_live_inventory_evidence", "generation_inventory",
    "inventory_assets", "json_sha256", "load_jsonl",
    "merge_day_records", "merge_message_records", "normalize_message",
    "parse_markdown_markers", "preflight_asset_capacity", "render_day",
    "render_message", "required_render_sections", "resolve_asset_sizes",
    "renderer_pointer_accounting", "sanitize_lossless_source", "source_field_census",
    "validate_record", "verify_generation",
]
