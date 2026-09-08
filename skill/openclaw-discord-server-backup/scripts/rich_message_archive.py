#!/usr/bin/env python3
"""Deterministic, loss-aware storage primitives for Discord message archives.

This module deliberately has no Discord bot client.  Callers provide API message
objects and, when binary preservation is requested, a bounded ``AssetDownloader``.
The canonical record retains independent content revisions and mutable
observations.  A checksummed ``CURRENT.json`` selects one complete generation;
individual raw/canonical trees are never selected independently.

Offline bytes and stored receipts can prove local integrity, but never live
completeness.  A runtime collector must perform bounded Discord pagination and
issue an in-process evidence capability before the full-history verifier may
return PASS.
"""
from __future__ import annotations

import fcntl
import errno
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
import weakref
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


RECORD_SCHEMA = "openclaw-discord-rich-message.v1"
RICH_CORE_CONTRACT = "openclaw-discord-rich-storage-core.v3"
POINTER_SCHEMA = "openclaw-discord-rich-current.v1"
JOURNAL_SCHEMA = "openclaw-discord-rich-journal.v1"
GENERATION_MANIFEST_SCHEMA = "openclaw-discord-rich-generation.v1"
ENTRY_RECEIPT_SCHEMA = "openclaw-discord-rich-entry-receipt.v2"
LIVE_EVIDENCE_SCHEMA = "openclaw-discord-rich-live-evidence.v2"
DISCORD_PAGE_RESPONSE_SCHEMA = "openclaw-discord-page-response.v1"
DISCORD_INVENTORY_RESPONSE_SCHEMA = "openclaw-discord-inventory-response.v1"
FULL_RUN_RECEIPT_SCHEMA = "openclaw-discord-full-rebuild-run.v1"
ASSET_RESERVATION_SCHEMA = "openclaw-discord-full-run-asset-reservation.v1"
STAGE_BASE_SCHEMA = "openclaw-discord-stage-base-current.v1"
SOURCE_CENSUS_SCHEMA = "openclaw-discord-source-census.v2"
CANONICAL_ARCHIVE_LOCK_NAME = ".channel_backup.lock"
DEFAULT_LIVE_EVIDENCE_TTL_SECONDS = 300.0
MAX_LIVE_EVIDENCE_TTL_SECONDS = 24 * 60 * 60.0
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


class AssetContentLengthMismatch(AssetDownloadError):
    """A bounded HTTP representation disagrees with Discord's recorded size."""

    def __init__(self, expected_size: int, observed_size: int) -> None:
        super().__init__("asset Content-Length does not match declared size")
        self.expected_size = expected_size
        self.observed_size = observed_size


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


def _attachment_equivalence_key(asset: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Bind direct and proxy URLs emitted by the same Discord attachment."""
    kind = asset.get("kind")
    pointer = str(asset.get("jsonPointer") or "")
    if kind == "attachment" and pointer.endswith("/url"):
        parent = pointer[:-4]
    elif kind == "attachment_proxy" and pointer.endswith("/proxy_url"):
        parent = pointer[:-10]
    else:
        return None
    return (
        parent,
        asset.get("displayFilename"),
        asset.get("sourceDeclaredSize"),
        asset.get("contentType"),
    )


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
            size_source = asset.get("sizeSource")
            source_size_mismatch = asset.get("sourceSizeMismatch")
            if expected_size is not None:
                exact_discord_size = (
                    actual_size == expected_size
                    and size_source in (None, "discord_payload")
                    and source_size_mismatch is None
                )
                verified_current_representation = (
                    isinstance(actual_size, int)
                    and not isinstance(actual_size, bool)
                    and actual_size >= 0
                    and actual_size != expected_size
                    and size_source == "http_get_content_length"
                    and source_size_mismatch is True
                )
                if not exact_discord_size and not verified_current_representation:
                    raise RichArchiveError("Discord-declared asset size was altered")
            elif actual_size is not None and (
                not isinstance(actual_size, int)
                or isinstance(actual_size, bool)
                or actual_size < 0
                or size_source != "http_head"
            ):
                raise RichArchiveError("asset size without Discord metadata lacks verified HEAD provenance")
            elif source_size_mismatch is not None:
                raise RichArchiveError("asset size mismatch receipt lacks Discord provenance")
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
        actual_by_id = {str(asset.get("assetId") or ""): asset for asset in actual_assets}
        for asset in actual_assets:
            recovery_method = asset.get("recoveryMethod")
            recovery_source_id = asset.get("recoveredFromAssetId")
            recovery_original_error = asset.get("recoveryOriginalError")
            if (
                recovery_method is None
                and recovery_source_id is None
                and recovery_original_error is None
            ):
                continue
            if (
                recovery_method != "equivalent_discord_attachment_variant"
                or not isinstance(recovery_source_id, str)
                or not recovery_source_id
                or not isinstance(recovery_original_error, str)
                or not recovery_original_error
                or len(recovery_original_error) > 1024
            ):
                raise RichArchiveError("asset recovery method is invalid")
            source_asset = actual_by_id.get(recovery_source_id)
            if (
                source_asset is None
                or source_asset is asset
                or source_asset.get("recoveryMethod") is not None
                or source_asset.get("recoveredFromAssetId") is not None
                or source_asset.get("recoveryOriginalError") is not None
                or _attachment_equivalence_key(source_asset) is None
                or _attachment_equivalence_key(source_asset)
                != _attachment_equivalence_key(asset)
                or source_asset.get("kind") == asset.get("kind")
                or source_asset.get("status") != "complete"
                or source_asset.get("sourceDeclaredSize") != source_asset.get("byteLength")
                or source_asset.get("sizeSource") not in (None, "discord_payload")
                or source_asset.get("sourceSizeMismatch") is not None
                or source_asset.get("byteLength") != asset.get("byteLength")
                or source_asset.get("sha256") != asset.get("sha256")
            ):
                raise RichArchiveError("asset recovery provenance is invalid")
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


def canonical_archive_lock_path(archive_root: Path) -> Path:
    """Return the one core-owned cross-process mutation lock for an archive."""
    archive_root = _lexical_absolute(archive_root)
    reject_symlink_path(archive_root)
    return contained_path(archive_root, CANONICAL_ARCHIVE_LOCK_NAME)


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
    remaining_elapsed_seconds: float
    _mutex: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_limits(cls, limits: AssetLimits) -> "AssetProbeBudget":
        return cls(
            remaining_requests=limits.max_unknown_size_probes,
            remaining_elapsed_seconds=limits.metadata_probe_elapsed_seconds,
        )

    def ensure_capacity(self, count: int) -> None:
        with self._mutex:
            if count > self.remaining_requests:
                raise AssetDownloadError("unknown-size asset metadata probe quota exceeded")
            if count and self.remaining_elapsed_seconds <= 0:
                raise AssetDownloadError("asset metadata probe elapsed-time cap exceeded")

    def consume(self) -> None:
        with self._mutex:
            self.ensure_capacity(1)
            self.remaining_requests -= 1

    def charge_elapsed(self, elapsed_seconds: float) -> None:
        """Charge only time spent performing a metadata probe, not batch idle time."""
        if elapsed_seconds < 0:
            raise AssetDownloadError("asset metadata probe elapsed time is invalid")
        with self._mutex:
            self.remaining_elapsed_seconds -= elapsed_seconds
            if self.remaining_elapsed_seconds < 0:
                raise AssetDownloadError("asset metadata probe elapsed-time cap exceeded")


@dataclass
class AssetRunBudget:
    """Cumulative file/byte/probe budget shared across every rebuild entry."""

    limits: AssetLimits
    probe_budget: AssetProbeBudget
    file_count: int = 0
    declared_bytes: int = 0
    _mutex: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_limits(cls, limits: AssetLimits) -> "AssetRunBudget":
        return cls(limits=limits, probe_budget=AssetProbeBudget.from_limits(limits))

    def preflight_entry(
        self,
        assets: Sequence[Mapping[str, Any]],
        destination_root: Path,
        *,
        assume_unknown_max: bool,
        assets_materialized: bool = False,
    ) -> dict[str, int]:
        with self._mutex:
            return preflight_asset_capacity(
                assets,
                destination_root,
                limits=self.limits,
                run_file_count=self.file_count,
                run_declared_bytes=self.declared_bytes,
                assume_unknown_max=assume_unknown_max,
                assets_materialized=assets_materialized,
            )

    def commit_entry(self, result: Mapping[str, Any]) -> None:
        files = result.get("files")
        declared = result.get("declaredBytes")
        if (
            not isinstance(files, int)
            or isinstance(files, bool)
            or files < 0
            or not isinstance(declared, int)
            or isinstance(declared, bool)
            or declared < 0
        ):
            raise AssetDownloadError("asset run budget cannot commit invalid preflight totals")
        with self._mutex:
            next_file_count = self.file_count + files
            next_declared_bytes = self.declared_bytes + declared
            if (
                next_file_count > self.limits.full_run_files
                or next_declared_bytes > self.limits.full_run_bytes
            ):
                raise AssetDownloadError("full-run asset quota exceeded")
            self.file_count = next_file_count
            self.declared_bytes = next_declared_bytes

    def reserve_entry(
        self,
        assets: Sequence[Mapping[str, Any]],
        destination_root: Path,
        *,
        assume_unknown_max: bool,
        assets_materialized: bool = False,
    ) -> dict[str, int]:
        """Atomically preflight and consume one entry's cumulative quota."""
        with self._mutex:
            result = preflight_asset_capacity(
                assets,
                destination_root,
                limits=self.limits,
                run_file_count=self.file_count,
                run_declared_bytes=self.declared_bytes,
                assume_unknown_max=assume_unknown_max,
                assets_materialized=assets_materialized,
            )
            self.commit_entry(result)
            return result

    def release_entry(self, result: Mapping[str, Any]) -> None:
        """Roll back a prior file/byte reservation without restoring probes."""
        files = result.get("files")
        declared = result.get("declaredBytes")
        if (
            not isinstance(files, int)
            or isinstance(files, bool)
            or files < 0
            or not isinstance(declared, int)
            or isinstance(declared, bool)
            or declared < 0
        ):
            raise AssetDownloadError("asset run budget cannot release invalid totals")
        with self._mutex:
            if files > self.file_count or declared > self.declared_bytes:
                raise AssetDownloadError("asset run budget release would underflow")
            self.file_count -= files
            self.declared_bytes -= declared


def preflight_asset_capacity(
    assets: Sequence[Mapping[str, Any]],
    destination_root: Path,
    *,
    limits: AssetLimits,
    run_file_count: int = 0,
    run_declared_bytes: int = 0,
    assume_unknown_max: bool = False,
    assets_materialized: bool = False,
) -> dict[str, int]:
    if not isinstance(assets_materialized, bool):
        raise AssetDownloadError("asset materialization state must be boolean")
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
    required = limits.disk_reserve_bytes if assets_materialized else declared + limits.disk_reserve_bytes
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
            if content_length is None or not content_length.isdigit():
                _close_response_safely(response)
                raise AssetDownloadError("asset Content-Length does not match declared size")
            observed_size = int(content_length)
            if observed_size > self.limits.per_file_bytes:
                _close_response_safely(response)
                raise AssetDownloadError("asset exceeds per-file size limit")
            if observed_size != expected_size:
                _close_response_safely(response)
                raise AssetContentLengthMismatch(expected_size, observed_size)
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
                started = time.monotonic()
                try:
                    row["declaredSize"] = downloader.probe_size(row)
                finally:
                    budget.charge_elapsed(max(0.0, time.monotonic() - started))
                row["sizeSource"] = "http_head"
            output.append(row)
        observation["assetInventory"] = output
    updated["observations"] = observations
    return updated


def _atomic_copy_equivalent_asset(
    source: Path,
    target: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    source = _lexical_absolute(source)
    target = _lexical_absolute(target)
    reject_symlink_path(source)
    reject_symlink_path(target)
    if os.path.lexists(target):
        raise AssetDownloadError("equivalent asset target already exists")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    reject_symlink_path(target.parent)
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    descriptor = -1
    temporary = ""
    digest = hashlib.sha256()
    count = 0
    try:
        source_info = os.fstat(source_fd)
        if (
            not stat.S_ISREG(source_info.st_mode)
            or source_info.st_nlink != 1
            or source_info.st_size != expected_size
        ):
            raise AssetDownloadError("equivalent asset source is unsafe")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".equivalent", dir=target.parent,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > expected_size:
                    raise AssetDownloadError("equivalent asset source exceeded declared size")
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if count != expected_size or digest.hexdigest() != expected_sha256:
            raise AssetDownloadError("equivalent asset source digest mismatch")
        os.replace(temporary, target)
        temporary = ""
        os.chmod(target, 0o600)
        _fsync_dir(target.parent)
    finally:
        os.close(source_fd)
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _recover_equivalent_attachment_variants(
    assets: Sequence[Mapping[str, Any]],
    generation_root: Path,
) -> list[dict[str, Any]]:
    output = [dict(asset) for asset in assets]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for asset in output:
        key = _attachment_equivalence_key(asset)
        if key is not None and asset.get("inScope"):
            groups.setdefault(key, []).append(asset)
    for rows in groups.values():
        complete = [row for row in rows if row.get("status") == "complete"]
        failed = [row for row in rows if row.get("status") == "error"]
        if not complete or not failed:
            continue
        complete.sort(key=lambda row: str(row.get("assetId") or ""))
        source = complete[0]
        if any(
            row.get("byteLength") != source.get("byteLength")
            or row.get("sha256") != source.get("sha256")
            for row in complete[1:]
        ):
            raise AssetDownloadError("equivalent attachment variants disagree")
        expected_size = source.get("byteLength")
        expected_sha256 = str(source.get("sha256") or "")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or source.get("sourceDeclaredSize") != expected_size
            or source.get("sizeSource") not in (None, "discord_payload")
            or source.get("sourceSizeMismatch") is not None
        ):
            raise AssetDownloadError("equivalent attachment source receipt is invalid")
        source_path = contained_path(
            generation_root, str(source.get("localRelativePath") or ""),
        )
        for row in failed:
            target_path = contained_path(
                generation_root, str(row.get("localRelativePath") or ""),
            )
            original_error = str(row.get("error") or "asset_download_failed")
            _atomic_copy_equivalent_asset(
                source_path,
                target_path,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            row.update({
                "status": "complete",
                "byteLength": expected_size,
                "sha256": expected_sha256,
                "error": None,
                "recoveryMethod": "equivalent_discord_attachment_variant",
                "recoveredFromAssetId": str(source["assetId"]),
                "recoveryOriginalError": original_error,
            })
    return output


def _retry_verified_discord_size_drift(
    assets: Sequence[Mapping[str, Any]],
    observed_sizes: Mapping[str, int],
    downloader: AssetDownloader,
    generation_root: Path,
) -> list[dict[str, Any]]:
    """Retry both representations only after their bounded GET sizes are known."""
    output = [dict(asset) for asset in assets]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for asset in output:
        key = _attachment_equivalence_key(asset)
        if key is not None and asset.get("inScope"):
            groups.setdefault(key, []).append(asset)
    for rows in groups.values():
        kinds = {str(row.get("kind") or "") for row in rows}
        if len(rows) != 2 or kinds != {"attachment", "attachment_proxy"}:
            continue
        if any(
            row.get("status") != "error"
            or str(row.get("assetId") or "") not in observed_sizes
            for row in rows
        ):
            continue
        candidates: list[dict[str, Any]] = []
        for row in rows:
            candidate = dict(row)
            observed_size = observed_sizes[str(row["assetId"])]
            if (
                not isinstance(observed_size, int)
                or isinstance(observed_size, bool)
                or observed_size < 0
                or observed_size > downloader.limits.per_file_bytes
                or observed_size == row.get("sourceDeclaredSize")
            ):
                raise AssetDownloadError("asset size-drift receipt is invalid")
            candidate.update({
                "declaredSize": observed_size,
                "sizeSource": "http_get_content_length",
                "sourceSizeMismatch": True,
                "status": "pending",
                "error": None,
            })
            candidates.append(candidate)
        preflight_asset_capacity(
            candidates,
            generation_root,
            limits=downloader.limits,
            assume_unknown_max=False,
        )
        replacements: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            try:
                replacement = downloader.download(candidate, generation_root)
            except AssetDownloadError as exc:
                replacement = dict(candidate)
                replacement.update({"status": "error", "error": str(exc)})
            replacements[str(candidate["assetId"])] = replacement
        output = [replacements.get(str(row.get("assetId") or ""), row) for row in output]
    return output


def apply_asset_results(record: Mapping[str, Any], downloader: AssetDownloader, generation_root: Path) -> dict[str, Any]:
    updated = dict(record)
    observations = [dict(row) for row in updated.get("observations") or []]
    for observation in observations:
        output: list[dict[str, Any]] = []
        observed_sizes: dict[str, int] = {}
        for asset in observation.get("assetInventory") or []:
            try:
                output.append(downloader.download(asset, generation_root))
            except AssetContentLengthMismatch as exc:
                failed = dict(asset)
                failed.update({"status": "error", "error": str(exc)})
                output.append(failed)
                observed_sizes[str(asset.get("assetId") or "")] = exc.observed_size
            except AssetDownloadError as exc:
                failed = dict(asset)
                failed.update({"status": "error", "error": str(exc)})
                output.append(failed)
        output = _retry_verified_discord_size_drift(
            output, observed_sizes, downloader, generation_root,
        )
        observation["assetInventory"] = _recover_equivalent_attachment_variants(
            output, generation_root,
        )
    updated["observations"] = observations
    remaining_errors = [
        f"{asset.get('assetId')}:{asset.get('error') or 'asset_download_failed'}"
        for observation in observations
        for asset in observation.get("assetInventory") or []
        if asset.get("inScope") and asset.get("status") == "error"
    ]
    updated["attachmentErrors"] = sorted(set(remaining_errors))
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


def _resume_stable_live_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only Discord CDN signature churn from a validated live binding."""
    expected_keys = {
        "messageId", "channelId", "apiSourcePayloadSha256", "sourcePayloadSha256",
        "visiblePayloadSha256", "sourceCensusSha256", "rendererAccountingSha256",
        "visiblePointerCount", "assets", "assetInventorySha256",
    }
    if set(value) != expected_keys or not isinstance(value.get("assets"), list):
        raise GenerationError("resume live binding shape is invalid")
    stable_assets: list[dict[str, Any]] = []
    for raw in value["assets"]:
        if not isinstance(raw, Mapping):
            raise GenerationError("resume asset binding shape is invalid")
        asset = dict(raw)
        remote = asset.get("remoteUrl")
        stable = asset.get("stableRemoteUrl")
        if _discord_url_without_signature(remote) != stable:
            raise GenerationError("resume asset URL is not bound to its stable identity")
        asset["remoteUrl"] = stable
        stable_assets.append(asset)
    return {
        "messageId": value["messageId"],
        "channelId": value["channelId"],
        "sourcePayloadSha256": value["sourcePayloadSha256"],
        "visiblePayloadSha256": value["visiblePayloadSha256"],
        "rendererAccountingSha256": value["rendererAccountingSha256"],
        "visiblePointerCount": value["visiblePointerCount"],
        "assets": stable_assets,
        "stableAssetInventorySha256": json_sha256(stable_assets),
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


def _inventory_identities(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    identities: list[dict[str, str]] = []
    channels: set[str] = set()
    paths: set[str] = set()
    for row in rows:
        relative_path = str(row.get("relativePath") or "")
        identity = _validated_entry_identity({
            "channelId": str(row.get("channelId") or ""),
            "relativePath": relative_path,
            "normalizedRelativePath": unicodedata.normalize("NFKC", relative_path).casefold(),
        })
        if identity["channelId"] in channels or identity["normalizedRelativePath"] in paths:
            raise GenerationError("fresh inventory contains duplicate channel or normalized path")
        channels.add(identity["channelId"])
        paths.add(identity["normalizedRelativePath"])
        identities.append(identity)
    return sorted(identities, key=lambda row: (int(row["channelId"]), row["normalizedRelativePath"]))


def _source_page(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [sanitize_lossless_source(dict(message)) for message in messages]


def _validated_inventory_response(value: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Require explicit completeness metadata from the Discord inventory adapter."""
    if not isinstance(value, Mapping):
        raise GenerationError("authoritative Discord inventory response is required")
    request = value.get("request")
    entries_value = value.get("entries")
    if (
        value.get("schemaVersion") != DISCORD_INVENTORY_RESPONSE_SCHEMA
        or value.get("source") != "discord-api-runtime-inventory"
        or value.get("complete") is not True
        or value.get("truncated") is not False
        or value.get("terminalPageObserved") is not True
        or value.get("activeChannelsComplete") is not True
        or value.get("activeThreadsComplete") is not True
        or value.get("archivedThreadsComplete") is not True
        or not isinstance(request, Mapping)
        or request.get("includeActiveThreads") is not True
        or request.get("includeArchivedThreads") is not True
        or not isinstance(entries_value, list)
        or any(not isinstance(row, Mapping) for row in entries_value)
        or not isinstance(value.get("responseCount"), int)
        or isinstance(value.get("responseCount"), bool)
        or value.get("responseCount") != len(entries_value)
        or not isinstance(value.get("pageCount"), int)
        or isinstance(value.get("pageCount"), bool)
        or value.get("pageCount") < 1
    ):
        raise GenerationError("Discord inventory response is partial, truncated, or malformed")
    identities = _inventory_identities([dict(row) for row in entries_value])
    canonical = {
        "schemaVersion": DISCORD_INVENTORY_RESPONSE_SCHEMA,
        "source": "discord-api-runtime-inventory",
        "request": {
            "includeActiveThreads": True,
            "includeArchivedThreads": True,
        },
        "complete": True,
        "truncated": False,
        "terminalPageObserved": True,
        "activeChannelsComplete": True,
        "activeThreadsComplete": True,
        "archivedThreadsComplete": True,
        "pageCount": int(value["pageCount"]),
        "responseCount": len(identities),
        "entries": identities,
    }
    return canonical, identities


def _validated_page_response(
    value: Any,
    *,
    channel_id: str,
    before: str | None,
    limit: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate one exact adapter response, including anti-truncation metadata."""
    if not isinstance(value, Mapping):
        raise GenerationError("authoritative Discord page response is required")
    request = value.get("request")
    messages_value = value.get("messages")
    if (
        value.get("schemaVersion") != DISCORD_PAGE_RESPONSE_SCHEMA
        or value.get("source") != "discord-api-runtime-page"
        or value.get("complete") is not True
        or value.get("truncated") is not False
        or not isinstance(request, Mapping)
        or not isinstance(request.get("channelId"), str)
        or request.get("channelId") != channel_id
        or request.get("before") != before
        or not isinstance(request.get("limit"), int)
        or isinstance(request.get("limit"), bool)
        or request.get("limit") != limit
        or not isinstance(messages_value, list)
        or any(not isinstance(row, Mapping) for row in messages_value)
        or not isinstance(value.get("responseCount"), int)
        or isinstance(value.get("responseCount"), bool)
        or value.get("responseCount") != len(messages_value)
        or len(messages_value) > limit
    ):
        raise GenerationError("Discord page response is partial, truncated, or request-mismatched")
    sources = _source_page([dict(row) for row in messages_value])
    canonical = {
        "schemaVersion": DISCORD_PAGE_RESPONSE_SCHEMA,
        "source": "discord-api-runtime-page",
        "request": {
            "channelId": channel_id,
            "before": before,
            "limit": limit,
        },
        "complete": True,
        "truncated": False,
        "responseCount": len(sources),
        "messages": sources,
    }
    return canonical, sources


_RUN_CONTEXT_GUARD = object()
_RUN_CONTEXT_REGISTRY: dict[str, dict[str, Any]] = {}
_RUN_CONTEXT_REGISTRY_MUTEX = threading.RLock()
_ACTIVE_ARCHIVE_ROOT_RUNS: dict[str, str] = {}
_ACTIVE_ENTRY_ROOT_RUNS: dict[str, str] = {}


def _remove_run_context_registration_locked(
    nonce: str,
    *,
    expected_context: ArchiveRunContext | None = None,
) -> dict[str, Any] | None:
    """Remove an idle run while its canonical lock remains run-pinned."""
    registration = _RUN_CONTEXT_REGISTRY.get(nonce)
    if registration is None:
        if expected_context is not None:
            expected_context._closed = True
        return None
    context = registration.get("context")()
    if expected_context is not None and context is not expected_context:
        return None
    if registration.get("borrowCount", 0) > 0:
        registration["closing"] = True
        return None
    _RUN_CONTEXT_REGISTRY.pop(nonce, None)
    archive_key = str(registration["archiveRoot"])
    if _ACTIVE_ARCHIVE_ROOT_RUNS.get(archive_key) == nonce:
        _ACTIVE_ARCHIVE_ROOT_RUNS.pop(archive_key, None)
    for entry_root in registration["entryRoots"].values():
        entry_key = str(entry_root)
        if _ACTIVE_ENTRY_ROOT_RUNS.get(entry_key) == nonce:
            _ACTIVE_ENTRY_ROOT_RUNS.pop(entry_key, None)
    if context is not None:
        context._closed = True
    return registration["lockRunPin"]


def _release_run_context_registration(
    nonce: str,
    *,
    expected_context: ArchiveRunContext | None = None,
) -> bool:
    """Release an idle run, or defer release until active operations finish."""
    with _RUN_CONTEXT_REGISTRY_MUTEX:
        existed = nonce in _RUN_CONTEXT_REGISTRY
        lock_run_pin = _remove_run_context_registration_locked(
            nonce,
            expected_context=expected_context,
        )
        removed = existed and nonce not in _RUN_CONTEXT_REGISTRY
        if not existed and expected_context is not None:
            expected_context._closed = True
            removed = True
    # The run registry and entry ownership are gone before the pin can release
    # the canonical flock.  This preserves the run-registry -> lock-registry order.
    if lock_run_pin is not None:
        _release_run_lock_pin(lock_run_pin)
    return removed


class ArchiveRunContext:
    """Opaque module-minted capability owning one run-wide asset budget."""

    __slots__ = ("_nonce", "_pid", "_kind", "_closed", "__weakref__")

    def __init__(self, *, kind: str, guard: object) -> None:
        if guard is not _RUN_CONTEXT_GUARD or kind not in {"full_rebuild", "incremental"}:
            raise RichArchiveError("archive run context cannot be constructed externally")
        self._nonce = secrets.token_hex(32)
        self._pid = os.getpid()
        self._kind = kind
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        _release_run_context_registration(
            self._nonce,
            expected_context=self,
        )

    def __enter__(self) -> "ArchiveRunContext":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


class FullRebuildRunContext(ArchiveRunContext):
    __slots__ = ()


class IncrementalRunContext(ArchiveRunContext):
    __slots__ = ()


def _mint_run_context(
    *,
    kind: str,
    entries: Sequence[Mapping[str, str]],
    archive_root: Path,
    lock_token: ArchiveLockToken,
    limits: AssetLimits,
    inventory_response: Mapping[str, Any] | None,
    expected_entries: Sequence[Mapping[str, str]] | None = None,
) -> ArchiveRunContext:
    archive_root = _lexical_absolute(archive_root)
    reject_symlink_path(archive_root)
    lock_identity = _validated_lock_token_identity(lock_token)
    canonical_lock_path = canonical_archive_lock_path(archive_root)
    if lock_identity["path"] != canonical_lock_path:
        raise RichArchiveError(
            "archive run requires the core-derived canonical backup lock path"
        )
    identities = [dict(row) for row in entries]
    digest = json_sha256(identities)
    expected_identities = [dict(row) for row in (expected_entries or identities)]
    entry_roots = {
        (row["channelId"], row["normalizedRelativePath"]): contained_path(
            archive_root, row["relativePath"],
        )
        for row in expected_identities
    }
    archive_key = str(archive_root)
    entry_keys = [str(root) for root in entry_roots.values()]
    with _RUN_CONTEXT_REGISTRY_MUTEX:
        if archive_key in _ACTIVE_ARCHIVE_ROOT_RUNS:
            raise RichArchiveError("archive root already has an active backup run")
        if any(key in _ACTIVE_ENTRY_ROOT_RUNS for key in entry_keys):
            raise RichArchiveError("archive entry already belongs to an active backup run")
        lock_run_pin = _pin_lock_token_for_run(lock_token)
        try:
            context_type = FullRebuildRunContext if kind == "full_rebuild" else IncrementalRunContext
            context = context_type(kind=kind, guard=_RUN_CONTEXT_GUARD)
            context_nonce = context._nonce
            context_ref = weakref.ref(
                context,
                lambda _reference, nonce=context_nonce: _release_run_context_registration(nonce),
            )
            _RUN_CONTEXT_REGISTRY[context._nonce] = {
                "context": context_ref,
                "pid": os.getpid(),
                "kind": kind,
                "runContextId": secrets.token_hex(32),
                "archiveRoot": archive_root,
                "archiveRootSha256": json_sha256(str(archive_root)),
                "lockToken": lock_token,
                "lockRunPin": lock_run_pin,
                "lockTokenNonce": lock_identity["nonce"],
                "lockPath": lock_identity["path"],
                "lockPathSha256": json_sha256(str(lock_identity["path"])),
                "lockDevice": lock_identity["device"],
                "lockInode": lock_identity["inode"],
                "entries": identities,
                "expectedEntries": expected_identities,
                "expectedEntriesDigest": json_sha256(expected_identities),
                "entryKeys": {
                    (row["channelId"], row["normalizedRelativePath"])
                    for row in expected_identities
                },
                "entryRoots": entry_roots,
                "inventoryDigest": digest,
                "inventoryResponse": (
                    json.loads(json.dumps(inventory_response, ensure_ascii=False))
                    if inventory_response is not None else None
                ),
                "mutex": threading.RLock(),
                "borrowCount": 0,
                "borrowOwners": {},
                "closing": False,
                "budget": AssetRunBudget.from_limits(limits),
                "assetReservations": {},
                "usedAssetReservations": set(),
                "processed": {},
                "sealedRoots": {},
            }
            _ACTIVE_ARCHIVE_ROOT_RUNS[archive_key] = context._nonce
            for key in entry_keys:
                _ACTIVE_ENTRY_ROOT_RUNS[key] = context._nonce
        except BaseException:
            _release_run_lock_pin(lock_run_pin, request_close=False)
            raise
    return context


def begin_full_rebuild_run(
    *,
    fetch_inventory: Callable[[], Mapping[str, Any]],
    expected_entries: Sequence[Mapping[str, Any]],
    archive_root: Path,
    lock_token: ArchiveLockToken,
    limits: AssetLimits | None = None,
) -> FullRebuildRunContext:
    expected = _inventory_identities(expected_entries)
    response, entries = _validated_inventory_response(fetch_inventory())
    if entries != expected:
        raise GenerationError(
            "authoritative Discord inventory does not equal the independent expected entry set"
        )
    context = _mint_run_context(
        kind="full_rebuild",
        entries=entries,
        expected_entries=expected,
        archive_root=archive_root,
        lock_token=lock_token,
        limits=limits or AssetLimits(),
        inventory_response=response,
    )
    assert isinstance(context, FullRebuildRunContext)
    return context


def begin_incremental_run(
    *,
    entries: Sequence[Mapping[str, Any]],
    archive_root: Path,
    lock_token: ArchiveLockToken,
    limits: AssetLimits | None = None,
) -> IncrementalRunContext:
    identities = _inventory_identities(entries)
    context = _mint_run_context(
        kind="incremental",
        entries=identities,
        archive_root=archive_root,
        lock_token=lock_token,
        limits=limits or AssetLimits(),
        inventory_response=None,
    )
    assert isinstance(context, IncrementalRunContext)
    return context


def _require_run_context(
    context: ArchiveRunContext | None,
    *,
    kind: str | None = None,
    identity: Mapping[str, str] | None = None,
    entry_root: Path | None = None,
    lock_token: ArchiveLockToken | None = None,
) -> dict[str, Any]:
    with _RUN_CONTEXT_REGISTRY_MUTEX:
        registration = (
            _RUN_CONTEXT_REGISTRY.get(context._nonce)
            if isinstance(context, ArchiveRunContext)
            else None
        )
        if (
            not isinstance(context, ArchiveRunContext)
            or context.closed
            or context._pid != os.getpid()
            or registration is None
            or registration.get("context")() is not context
            or registration.get("pid") != os.getpid()
            or registration.get("kind") != context._kind
            or (kind is not None and registration.get("kind") != kind)
            or _ACTIVE_ARCHIVE_ROOT_RUNS.get(str(registration["archiveRoot"])) != context._nonce
        ):
            raise RichArchiveError("valid module-minted archive run context is required")
        owner_count = registration.get("borrowOwners", {}).get(threading.get_ident(), 0)
        if registration.get("closing") and owner_count == 0:
            raise RichArchiveError("archive run context is closing")
        if identity is not None:
            key = (identity["channelId"], identity["normalizedRelativePath"])
            if key not in registration["entryKeys"]:
                raise RichArchiveError("archive entry is outside the run context inventory")
            if entry_root is not None and _lexical_absolute(entry_root) != registration["entryRoots"][key]:
                raise RichArchiveError("archive entry root does not match the run context identity")
        elif (
            entry_root is not None
            and _lexical_absolute(entry_root) not in registration["entryRoots"].values()
        ):
            raise RichArchiveError("archive entry root is outside the run context inventory")
        bound_token = registration["lockToken"]
        if lock_token is not None and lock_token is not bound_token:
            raise RichArchiveError("archive run mutation requires its bound lock token")
        lock_identity = _validated_lock_token_identity(
            bound_token,
            allow_closing_owner=True,
        )
        if (
            lock_identity["nonce"] != registration["lockTokenNonce"]
            or lock_identity["path"] != registration["lockPath"]
            or lock_identity["device"] != registration["lockDevice"]
            or lock_identity["inode"] != registration["lockInode"]
        ):
            raise RichArchiveError("archive run lock identity changed")
    return registration


def _begin_run_context_lease(
    context: ArchiveRunContext | None,
    *,
    kind: str,
) -> dict[str, Any]:
    """Borrow a run and its bound canonical lock for one run-wide decision."""
    owner = threading.get_ident()
    with _RUN_CONTEXT_REGISTRY_MUTEX:
        registration = _require_run_context(context, kind=kind)
        owners = registration["borrowOwners"]
        if registration.get("closing") and owners.get(owner, 0) == 0:
            raise RichArchiveError("archive run context is closing")
        owners[owner] = owners.get(owner, 0) + 1
        registration["borrowCount"] += 1
        lock_owner = RichArchiveStore(
            registration["archiveRoot"],
            lock_path=registration["lockPath"],
        )
        try:
            lock_lease = lock_owner._begin_lock_lease(registration["lockToken"])
        except BaseException:
            owners[owner] -= 1
            if owners[owner] == 0:
                owners.pop(owner)
            registration["borrowCount"] -= 1
            raise
        return {
            "nonce": context._nonce,
            "owner": owner,
            "registration": registration,
            "context": context,
            "lockOwner": lock_owner,
            "lockLease": lock_lease,
        }


def _end_run_context_lease(lease: Mapping[str, Any]) -> None:
    lock_run_pin: dict[str, Any] | None = None
    try:
        lease["lockOwner"]._end_lock_lease(lease["lockLease"])
    finally:
        with _RUN_CONTEXT_REGISTRY_MUTEX:
            nonce = str(lease["nonce"])
            registration = _RUN_CONTEXT_REGISTRY.get(nonce)
            if registration is not lease["registration"]:
                raise RichArchiveError("archive run lease registry changed during finalization")
            owners = registration["borrowOwners"]
            owner = int(lease["owner"])
            if owners.get(owner, 0) < 1 or registration.get("borrowCount", 0) < 1:
                raise RichArchiveError("archive run lease count underflow")
            owners[owner] -= 1
            if owners[owner] == 0:
                owners.pop(owner)
            registration["borrowCount"] -= 1
            if registration["borrowCount"] == 0 and registration.get("closing"):
                lock_run_pin = _remove_run_context_registration_locked(nonce)
    if lock_run_pin is not None:
        _release_run_lock_pin(lock_run_pin)


def finalize_full_rebuild_run(context: FullRebuildRunContext | None) -> dict[str, Any]:
    """Consume a full-run context and prove every inventoried entry published."""
    run_lease = _begin_run_context_lease(context, kind="full_rebuild")
    registration = run_lease["registration"]
    lock_token = registration["lockToken"]
    try:
        registration = _require_run_context(
            context,
            kind="full_rebuild",
            lock_token=lock_token,
        )
        expected = sorted(
            (row["channelId"] for row in registration["expectedEntries"]), key=int,
        )
        processed = sorted(registration["processed"], key=int)
        errors = [] if processed == expected else ["not_all_inventory_entries_published"]
        reservation_ids = {
            channel_id: str(receipt.get("reservationId") or "")
            for channel_id, receipt in registration["assetReservations"].items()
        }
        if (
            sorted(reservation_ids, key=int) != expected
            or set(reservation_ids.values()) != registration["usedAssetReservations"]
        ):
            errors.append("not_all_asset_reservations_consumed")
        current_hashes: dict[str, str | None] = {}
        for identity in registration["expectedEntries"]:
            channel_id = identity["channelId"]
            key = (channel_id, identity["normalizedRelativePath"])
            actual_hash: str | None = None
            try:
                current = RichArchiveStore(registration["entryRoots"][key]).resolve_current()
                if current is not None:
                    actual_hash = verify_generation(current)["generationSha256"]
            except (RichArchiveError, OSError, ValueError):
                errors.append(f"current_generation_readback_failed:{channel_id}")
            current_hashes[channel_id] = actual_hash
            if registration["processed"].get(channel_id) != actual_hash:
                errors.append(f"current_generation_hash_mismatch:{channel_id}")
        budget = registration["budget"]
        receipt: dict[str, Any] = {
            "schemaVersion": FULL_RUN_RECEIPT_SCHEMA,
            "gateStatus": "PASS" if not errors else "FAIL",
            "runContextId": registration["runContextId"],
            "archiveRootSha256": registration["archiveRootSha256"],
            "inventoryDigest": registration["inventoryDigest"],
            "expectedEntriesDigest": registration["expectedEntriesDigest"],
            "inventoryResponseSha256": json_sha256(registration["inventoryResponse"]),
            "expectedEntryCount": len(expected),
            "processedEntryCount": len(processed),
            "processedGenerationSha256ByChannel": dict(registration["processed"]),
            "assetReservationIdByChannel": reservation_ids,
            "currentGenerationSha256ByChannel": current_hashes,
            "assetFileCount": budget.file_count,
            "assetDeclaredBytes": budget.declared_bytes,
            "errors": sorted(set(errors)),
        }
        receipt["receiptSha256"] = json_sha256(receipt)
        return receipt
    finally:
        try:
            _end_run_context_lease(run_lease)
        finally:
            if isinstance(context, FullRebuildRunContext):
                context.close()


def verify_sealed_full_rebuild_run(
    context: FullRebuildRunContext | None,
) -> dict[str, Any]:
    """Verify every full-run generation is sealed before root selection.

    Unlike ``finalize_full_rebuild_run``, this gate intentionally leaves the
    run context and canonical lock live so the caller can atomically publish
    the archive-root selector followed by compatibility pointers.
    """
    run_lease = _begin_run_context_lease(context, kind="full_rebuild")
    try:
        registration = _require_run_context(context, kind="full_rebuild")
        expected = sorted(
            (row["channelId"] for row in registration["expectedEntries"]),
            key=int,
        )
        processed = sorted(registration["processed"], key=int)
        sealed_roots = registration.get("sealedRoots") or {}
        errors: list[str] = []
        if processed != expected:
            errors.append("not_all_inventory_entries_sealed")
        if sorted(sealed_roots, key=int) != expected:
            errors.append("not_all_sealed_roots_registered")
        verified_roots: dict[str, dict[str, str]] = {}
        for channel_id in expected:
            raw_root = sealed_roots.get(channel_id)
            try:
                root = _lexical_absolute(Path(str(raw_root)))
                local = verify_generation(root)
                expected_hash = registration["processed"].get(channel_id)
                if (
                    not local.get("ok")
                    or local.get("generationSha256") != expected_hash
                    or root.parent.name != "generations"
                ):
                    raise GenerationError("sealed generation verification mismatch")
                verified_roots[channel_id] = {
                    "generationId": root.name,
                    "generationSha256": str(expected_hash),
                }
            except (OSError, ValueError, RichArchiveError):
                errors.append(f"sealed_generation_readback_failed:{channel_id}")
        budget = registration["budget"]
        receipt: dict[str, Any] = {
            "schemaVersion": "openclaw-discord-rich-sealed-run.v1",
            "gateStatus": "PASS" if not errors else "FAIL",
            "runContextId": registration["runContextId"],
            "archiveRootSha256": registration["archiveRootSha256"],
            "inventoryDigest": registration["inventoryDigest"],
            "expectedEntriesDigest": registration["expectedEntriesDigest"],
            "expectedEntryCount": len(expected),
            "sealedEntryCount": len(verified_roots),
            "sealedGenerationByChannel": verified_roots,
            "assetFileCount": budget.file_count,
            "assetDeclaredBytes": budget.declared_bytes,
            "errors": sorted(set(errors)),
        }
        receipt["receiptSha256"] = json_sha256(receipt)
        return receipt
    finally:
        _end_run_context_lease(run_lease)


_LIVE_EVIDENCE_GUARD = object()
_LIVE_EVIDENCE_REGISTRY: dict[str, dict[str, Any]] = {}


class LiveEvidenceToken:
    """Opaque, expiring, one-transaction proof minted by the live collector."""

    __slots__ = ("_nonce", "_pid", "_closed", "__weakref__")

    def __init__(self, *, guard: object) -> None:
        if guard is not _LIVE_EVIDENCE_GUARD:
            raise RichArchiveError("live evidence token cannot be constructed externally")
        self._nonce = secrets.token_hex(32)
        self._pid = os.getpid()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def audit_evidence(self) -> dict[str, Any]:
        registration = _require_live_evidence_token(self, allowed_states={"fresh", "prepared"})
        return json.loads(json.dumps(registration["evidence"], ensure_ascii=False))

    def close(self) -> None:
        _consume_live_evidence_token(self)

    def __enter__(self) -> "LiveEvidenceToken":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


def _consume_live_evidence_token(token: LiveEvidenceToken | None) -> None:
    if not isinstance(token, LiveEvidenceToken):
        return
    registration = _LIVE_EVIDENCE_REGISTRY.get(token._nonce)
    if registration is not None and registration.get("token")() is token:
        registration["state"] = "consumed"
        _LIVE_EVIDENCE_REGISTRY.pop(token._nonce, None)
    token._closed = True


def _generation_binding(root: Path) -> tuple[Path, str]:
    absolute = _lexical_absolute(root)
    generation_id = _validated_generation_id(absolute.name)
    if absolute.parent.name not in {".staging", "generations"}:
        raise GenerationError("generation root is outside a managed entry transaction")
    return absolute.parent.parent, generation_id


def _require_live_evidence_token(
    token: LiveEvidenceToken | None,
    *,
    root: Path | None = None,
    allowed_states: set[str] | None = None,
) -> dict[str, Any]:
    registration = (
        _LIVE_EVIDENCE_REGISTRY.get(token._nonce)
        if isinstance(token, LiveEvidenceToken)
        else None
    )
    states = allowed_states or {"fresh", "prepared"}
    if (
        not isinstance(token, LiveEvidenceToken)
        or token.closed
        or token._pid != os.getpid()
        or registration is None
        or registration.get("token")() is not token
        or registration.get("pid") != os.getpid()
        or registration.get("state") not in states
    ):
        raise GenerationError("valid runtime live evidence token is required")
    if time.monotonic() > registration["expiresAtMonotonic"]:
        _consume_live_evidence_token(token)
        raise GenerationError("runtime live evidence token expired")
    run_context = registration["runContext"]()
    run_registration = _require_run_context(run_context, kind="full_rebuild")
    if (
        run_registration["runContextId"] != registration["runContextId"]
        or run_registration["archiveRoot"] != registration["archiveRoot"]
        or run_registration["archiveRootSha256"] != registration["archiveRootSha256"]
        or run_registration["expectedEntriesDigest"] != registration["expectedEntriesDigest"]
    ):
        raise GenerationError("runtime live evidence run context binding changed")
    if root is not None:
        entry_root, generation_id = _generation_binding(root)
        if (
            entry_root != registration["entryRoot"]
            or generation_id != registration["generationId"]
        ):
            raise GenerationError("runtime live evidence token targets another transaction")
    evidence = registration["evidence"]
    transaction = evidence.get("transactionBinding") or {}
    identity = evidence.get("entryIdentity") or {}
    if (
        evidence.get("evidenceSha256") != registration.get("evidenceSha256")
        or identity.get("channelId") != registration.get("channelId")
        or identity.get("relativePath") != registration.get("relativePath")
        or transaction.get("generationId") != registration.get("generationId")
        or transaction.get("runContextId") != registration.get("runContextId")
        or transaction.get("archiveRootSha256") != registration.get("archiveRootSha256")
        or transaction.get("inventoryDigest") != registration.get("inventoryDigest")
        or transaction.get("expectedEntriesDigest") != registration.get("expectedEntriesDigest")
        or transaction.get("verifiedCutoff") != registration.get("verifiedCutoff")
    ):
        raise GenerationError("runtime live evidence token binding changed")
    return registration


def collect_live_evidence(
    *,
    fetch_page: Callable[..., Mapping[str, Any]],
    verify_immutable_evidence: Callable[[], Mapping[str, Any]],
    run_context: FullRebuildRunContext,
    entry_root: Path,
    generation_id: str,
    channel_id: str,
    relative_path: str,
    page_limit: int = 100,
    max_pages: int = 100_000,
    max_messages: int = 10_000_000,
    evidence_ttl_seconds: float = DEFAULT_LIVE_EVIDENCE_TTL_SECONDS,
    allowed_cdn_hosts: frozenset[str] = DEFAULT_CDN_HOSTS,
) -> LiveEvidenceToken:
    """Perform bounded backward pagination and mint non-persistable PASS authority."""
    if (
        not str(channel_id).isdigit()
        or not isinstance(page_limit, int)
        or isinstance(page_limit, bool)
        or not 1 <= page_limit <= 100
    ):
        raise GenerationError("live collector channel or page limit is invalid")
    if (
        not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1
        or not isinstance(max_messages, int) or isinstance(max_messages, bool) or max_messages < 0
        or not isinstance(evidence_ttl_seconds, (int, float))
        or isinstance(evidence_ttl_seconds, bool)
        or not 0 < evidence_ttl_seconds <= MAX_LIVE_EVIDENCE_TTL_SECONDS
    ):
        raise GenerationError("live collector bounds are invalid")
    generation_id = _validated_generation_id(generation_id)
    entry_root = _lexical_absolute(entry_root)
    identity = _validated_entry_identity({
        "channelId": str(channel_id),
        "relativePath": relative_path,
        "normalizedRelativePath": unicodedata.normalize("NFKC", relative_path).casefold(),
    })
    run_registration = _require_run_context(
        run_context,
        kind="full_rebuild",
        identity=identity,
        entry_root=entry_root,
    )
    inventory_response = run_registration.get("inventoryResponse")
    if inventory_response is None:
        raise GenerationError("full rebuild context lacks authoritative inventory evidence")
    inventory, inventory_entries = _validated_inventory_response(inventory_response)
    if identity not in inventory_entries:
        raise GenerationError("full rebuild inventory does not contain the exact entry identity")
    immutable = _validate_immutable_evidence_reference(verify_immutable_evidence())
    fetch_started = datetime.now(timezone.utc).isoformat()

    cutoff_envelope, cutoff_sources = _validated_page_response(
        fetch_page(str(channel_id), before=None, limit=1),
        channel_id=str(channel_id),
        before=None,
        limit=1,
    )
    for source in cutoff_sources:
        normalize_message(
            source, expected_channel_id=str(channel_id), observed_at=fetch_started,
            allowed_cdn_hosts=allowed_cdn_hosts,
        )
    cutoff = str(cutoff_sources[0]["id"]) if cutoff_sources else None

    before = str(int(cutoff) + 1) if cutoff is not None else None
    raw_pages: list[dict[str, Any]] = []
    raw_messages: list[dict[str, Any]] = []
    seen: set[str] = set()
    terminal = False
    for page_index in range(max_pages):
        response_envelope, sources = _validated_page_response(
            fetch_page(str(channel_id), before=before, limit=page_limit),
            channel_id=str(channel_id),
            before=before,
            limit=page_limit,
        )
        ids = [str(source.get("id") or "") for source in sources]
        if any(not value.isdigit() for value in ids) or len(ids) != len(set(ids)):
            raise GenerationError("Discord page contains invalid or duplicate message IDs")
        if any(value in seen for value in ids):
            raise GenerationError("Discord pagination repeated a message ID")
        if before is not None and any(int(value) >= int(before) for value in ids):
            raise GenerationError("Discord before-pagination response crossed its request cursor")
        if cutoff is None and ids:
            raise GenerationError("empty cutoff observation changed during enumeration")
        if cutoff is not None and any(int(value) > int(cutoff) for value in ids):
            raise GenerationError("Discord page crossed the frozen cutoff")
        if ids != sorted(ids, key=int, reverse=True):
            raise GenerationError("Discord page message IDs are not strictly newest-first")
        seen.update(ids)
        raw_messages.extend(sources)
        terminal = not sources
        raw_pages.append({
            "pageIndex": page_index,
            "requestBefore": before,
            "requestLimit": page_limit,
            "responseEnvelope": response_envelope,
            "responseEnvelopeSha256": json_sha256(response_envelope),
            "terminal": terminal,
        })
        if len(raw_messages) > max_messages:
            raise GenerationError("live collector message bound exceeded")
        if terminal:
            break
        next_before = ids[-1]
        if before is not None and int(next_before) >= int(before):
            raise GenerationError("Discord pagination cursor did not move backward")
        before = next_before
    if not terminal:
        raise GenerationError("live collector page bound reached before terminal page")
    if cutoff is not None and cutoff not in seen:
        raise GenerationError("frozen cutoff message was absent from full enumeration")

    fetch_completed = datetime.now(timezone.utc).isoformat()
    normalized = [
        normalize_message(
            source,
            expected_channel_id=str(channel_id),
            observed_at=fetch_completed,
            allowed_cdn_hosts=allowed_cdn_hosts,
        )
        for source in sorted(raw_messages, key=lambda row: int(str(row["id"])))
    ]
    message_rows = [_active_live_binding(record) for record in normalized]
    body: dict[str, Any] = {
        "schemaVersion": LIVE_EVIDENCE_SCHEMA,
        "entryIdentity": identity,
        "inventory": {
            "complete": True,
            "entries": inventory_entries,
            "digest": run_registration["inventoryDigest"],
            "responseEnvelope": inventory,
            "responseEnvelopeSha256": json_sha256(inventory),
            "observedAt": fetch_started,
            "channelId": str(channel_id),
        },
        "transactionBinding": {
            "entryRootSha256": json_sha256(str(entry_root)),
            "generationId": generation_id,
            "runContextId": run_registration["runContextId"],
            "archiveRootSha256": run_registration["archiveRootSha256"],
            "inventoryDigest": run_registration["inventoryDigest"],
            "expectedEntriesDigest": run_registration["expectedEntriesDigest"],
            "channelId": str(channel_id),
            "relativePath": relative_path,
            "verifiedCutoff": cutoff,
        },
        "cutoffObservation": {
            "requestLimit": 1,
            "responseEnvelope": cutoff_envelope,
            "responseEnvelopeSha256": json_sha256(cutoff_envelope),
        },
        "verifiedCutoff": cutoff,
        "trulyEmpty": not message_rows,
        "enumeration": {
            "source": "discord-api-runtime-collector",
            "direction": "before",
            "complete": True,
            "terminalPageObserved": terminal,
            "pageLimit": page_limit,
            "pageCount": len(raw_pages),
            "fetchedMessageCount": len(message_rows),
            "fetchStartedAt": fetch_started,
            "fetchCompletedAt": fetch_completed,
            "pages": raw_pages,
            "pagePayloadSha256": json_sha256(raw_pages),
        },
        "messages": message_rows,
        "immutableEvidence": immutable,
    }
    body["evidenceSha256"] = json_sha256(body)
    token = LiveEvidenceToken(guard=_LIVE_EVIDENCE_GUARD)
    _LIVE_EVIDENCE_REGISTRY[token._nonce] = {
        "token": weakref.ref(token),
        "pid": os.getpid(),
        "state": "fresh",
        "evidence": body,
        # The normalized records remain process-local authority.  Persisted
        # evidence intentionally stores only active bindings; full rebuild
        # materialization consumes these exact records so the bytes written to
        # staging cannot drift from the live proof that authorized them.
        "normalizedRecords": json.loads(json.dumps(normalized, ensure_ascii=False)),
        "evidenceSha256": body["evidenceSha256"],
        "entryRoot": entry_root,
        "generationId": generation_id,
        "channelId": str(channel_id),
        "relativePath": relative_path,
        "verifiedCutoff": cutoff,
        "inventoryDigest": run_registration["inventoryDigest"],
        "expectedEntriesDigest": run_registration["expectedEntriesDigest"],
        "archiveRoot": run_registration["archiveRoot"],
        "archiveRootSha256": run_registration["archiveRootSha256"],
        "runContext": weakref.ref(run_context),
        "runContextId": run_registration["runContextId"],
        "expiresAtMonotonic": time.monotonic() + float(evidence_ttl_seconds),
    }
    return token


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


def _verified_generation_assets(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return unique verified in-scope assets and their immutable byte evidence."""
    records, _by_day, duplicate_ids = _load_generation_records(root)
    if duplicate_ids:
        raise AssetDownloadError("asset reservation rejects duplicate canonical IDs")
    unique: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for record in records.values():
        outcome = validate_record(record, generation_root=root)
        if outcome["attachmentErrors"]:
            raise AssetDownloadError("asset reservation requires verified local attachment bytes")
        for observation in record.get("observations") or []:
            for asset in observation.get("assetInventory") or []:
                if not asset.get("inScope"):
                    continue
                relative = str(asset.get("localRelativePath") or "")
                path = contained_path(root, relative)
                declared_size = asset.get("declaredSize")
                byte_length = asset.get("byteLength")
                digest = str(asset.get("sha256") or "")
                if (
                    asset.get("status") != "complete"
                    or not isinstance(declared_size, int)
                    or isinstance(declared_size, bool)
                    or declared_size < 0
                    or byte_length != declared_size
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not _regular_single_link(path)
                    or path.stat().st_size != byte_length
                    or file_sha256(path) != digest
                ):
                    raise AssetDownloadError("asset reservation encountered unverified attachment bytes")
                budget_asset = dict(asset)
                evidence = {
                    "assetId": str(asset.get("assetId") or ""),
                    "localRelativePath": relative,
                    "declaredSize": declared_size,
                    "byteLength": byte_length,
                    "sha256": digest,
                }
                previous = unique.get(relative)
                if previous is not None and previous[1] != evidence:
                    raise AssetDownloadError("asset reservation path has conflicting byte evidence")
                unique[relative] = (budget_asset, evidence)
    rows = [unique[key] for key in sorted(unique)]
    return [row[0] for row in rows], [row[1] for row in rows]


def _validated_persisted_asset_reservation(
    stage: Path,
    identity: Mapping[str, str],
) -> dict[str, Any]:
    entry_root, generation_id = _generation_binding(stage)
    relative_part_count = len(PurePosixPath(identity["relativePath"]).parts)
    derived_archive_root = entry_root.parents[relative_part_count - 1]
    receipt_path = contained_path(stage, "receipts/full-run-asset-reservation.json")
    if not _regular_single_link(receipt_path):
        raise AssetDownloadError("full-run asset reservation receipt is required")
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(persisted, Mapping):
        raise AssetDownloadError("full-run asset reservation receipt is invalid")
    body = dict(persisted)
    receipt_sha256 = body.pop("receiptSha256", None)
    if receipt_sha256 != json_sha256(body):
        raise AssetDownloadError("full-run asset reservation receipt checksum mismatch")
    assets, asset_evidence = _verified_generation_assets(stage)
    if (
        body.get("schemaVersion") != ASSET_RESERVATION_SCHEMA
        or not re.fullmatch(r"[0-9a-f]{64}", str(body.get("runContextId") or ""))
        or body.get("archiveRootSha256") != json_sha256(str(derived_archive_root))
        or not re.fullmatch(r"[0-9a-f]{64}", str(body.get("inventoryDigest") or ""))
        or body.get("entryIdentity") != dict(identity)
        or body.get("entryRootSha256") != json_sha256(str(entry_root))
        or body.get("generationId") != generation_id
        or body.get("assetFileCount") != len(assets)
        or body.get("assetDeclaredBytes") != sum(row["declaredSize"] for row in assets)
        or body.get("assets") != asset_evidence
        or body.get("assetsSha256") != json_sha256(asset_evidence)
    ):
        raise AssetDownloadError("full-run asset reservation no longer matches staged bytes")
    reservation_id = str(body.get("reservationId") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", reservation_id):
        raise AssetDownloadError("full-run asset reservation identity is invalid")
    return dict(persisted)


def _validated_stage_asset_reservation(
    stage: Path,
    run_registration: Mapping[str, Any],
    identity: Mapping[str, str],
    *,
    require_unused: bool,
) -> dict[str, Any]:
    entry_root, _generation_id = _generation_binding(stage)
    key = (identity["channelId"], identity["normalizedRelativePath"])
    if run_registration["entryRoots"].get(key) != entry_root:
        raise AssetDownloadError("asset reservation targets another archive entry root")
    registered = run_registration["assetReservations"].get(identity["channelId"])
    persisted = _validated_persisted_asset_reservation(stage, identity)
    if not isinstance(registered, Mapping) or dict(registered) != persisted:
        raise AssetDownloadError("module-minted full-run asset reservation binding is required")
    body = dict(persisted)
    if (
        body.get("runContextId") != run_registration["runContextId"]
        or body.get("archiveRootSha256") != run_registration["archiveRootSha256"]
        or body.get("inventoryDigest") != run_registration["inventoryDigest"]
    ):
        raise AssetDownloadError("full-run asset reservation run binding mismatch")
    reservation_id = str(body.get("reservationId") or "")
    if require_unused and reservation_id in run_registration["usedAssetReservations"]:
        raise AssetDownloadError("full-run asset reservation was already consumed")
    return persisted


def _validated_live_evidence(root: Path, evidence: Any, projection: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(evidence, Mapping) or evidence.get("schemaVersion") != LIVE_EVIDENCE_SCHEMA:
        raise GenerationError("full PASS live evidence schema is missing or unsupported")
    body = dict(evidence)
    supplied_digest = body.pop("evidenceSha256", None)
    if supplied_digest != json_sha256(body):
        raise GenerationError("live evidence checksum mismatch")
    identity = _validated_entry_identity(evidence.get("entryIdentity"))
    entry_root, generation_id = _generation_binding(root)
    relative_part_count = len(PurePosixPath(identity["relativePath"]).parts)
    derived_archive_root = entry_root.parents[relative_part_count - 1]
    transaction = evidence.get("transactionBinding")
    if not isinstance(transaction, Mapping):
        raise GenerationError("live evidence transaction binding is missing")
    inventory = evidence.get("inventory")
    if not isinstance(inventory, Mapping) or inventory.get("complete") is not True:
        errors.append("inventory_not_complete")
        inventory = {}
    try:
        inventory_response, inventory_entries = _validated_inventory_response(
            inventory.get("responseEnvelope")
        )
    except (RichArchiveError, TypeError):
        inventory_response = {}
        inventory_entries = []
        errors.append("inventory_entries_invalid")
    inventory_digest = json_sha256(inventory_entries)
    if (
        inventory.get("digest") != inventory_digest
        or inventory.get("entries") != inventory_entries
        or inventory.get("responseEnvelope") != inventory_response
        or inventory.get("responseEnvelopeSha256") != json_sha256(inventory_response)
        or str(inventory.get("channelId") or "") != identity["channelId"]
        or identity not in inventory_entries
    ):
        errors.append("inventory_identity_or_digest_invalid")
    if (
        transaction.get("entryRootSha256") != json_sha256(str(entry_root))
        or transaction.get("generationId") != generation_id
        or transaction.get("archiveRootSha256") != json_sha256(str(derived_archive_root))
        or transaction.get("inventoryDigest") != inventory_digest
        or transaction.get("expectedEntriesDigest") != inventory_digest
        or transaction.get("channelId") != identity["channelId"]
        or transaction.get("relativePath") != identity["relativePath"]
        or not re.fullmatch(r"[0-9a-f]{64}", str(transaction.get("runContextId") or ""))
    ):
        errors.append("transaction_binding_invalid")
    _iso_timestamp(inventory.get("observedAt"), field="inventory.observedAt", required=True)
    immutable = _validate_immutable_evidence_reference(evidence.get("immutableEvidence"))
    enumeration = evidence.get("enumeration")
    if not isinstance(enumeration, Mapping):
        raise GenerationError("live enumeration evidence is missing")
    messages = evidence.get("messages")
    if not isinstance(messages, list) or any(not isinstance(row, Mapping) for row in messages):
        raise GenerationError("live evidence messages are invalid")
    message_rows = [dict(row) for row in messages]
    if (
        enumeration.get("source") != "discord-api-runtime-collector"
        or enumeration.get("direction") != "before"
        or enumeration.get("complete") is not True
        or enumeration.get("terminalPageObserved") is not True
    ):
        errors.append("live_enumeration_incomplete")
    page_limit = enumeration.get("pageLimit")
    pages = enumeration.get("pages")
    if (
        not isinstance(page_limit, int) or isinstance(page_limit, bool)
        or not 1 <= page_limit <= 100
        or not isinstance(pages, list) or not pages
        or any(not isinstance(page, Mapping) for page in pages)
    ):
        errors.append("live_page_proof_missing")
        pages = []
        page_limit = 0
    if (
        enumeration.get("pageCount") != len(pages)
        or enumeration.get("pagePayloadSha256") != json_sha256(pages)
    ):
        errors.append("live_page_denominator_mismatch")
    started = _iso_timestamp(enumeration.get("fetchStartedAt"), field="fetchStartedAt", required=True)
    completed = _iso_timestamp(enumeration.get("fetchCompletedAt"), field="fetchCompletedAt", required=True)
    if started > completed:
        errors.append("live_fetch_time_invalid")
    cutoff_observation = evidence.get("cutoffObservation")
    cutoff_sources: list[dict[str, Any]] = []
    cutoff_envelope: dict[str, Any] = {}
    if isinstance(cutoff_observation, Mapping):
        try:
            cutoff_envelope, cutoff_sources = _validated_page_response(
                cutoff_observation.get("responseEnvelope"),
                channel_id=identity["channelId"],
                before=None,
                limit=1,
            )
        except RichArchiveError:
            errors.append("cutoff_observation_invalid")
    cutoff_ids = [str(row.get("id") or "") for row in cutoff_sources]
    if (
        not isinstance(cutoff_observation, Mapping)
        or cutoff_observation.get("requestLimit") != 1
        or cutoff_observation.get("responseEnvelope") != cutoff_envelope
        or cutoff_observation.get("responseEnvelopeSha256") != json_sha256(cutoff_envelope)
        or any(not value.isdigit() for value in cutoff_ids)
    ):
        errors.append("cutoff_observation_invalid")
    cutoff = evidence.get("verifiedCutoff")
    derived_cutoff = cutoff_ids[0] if cutoff_ids else None
    if cutoff != derived_cutoff or transaction.get("verifiedCutoff") != derived_cutoff:
        errors.append("verified_cutoff_mismatch")

    page_sources: list[dict[str, Any]] = []
    seen_page_ids: set[str] = set()
    expected_before = str(int(derived_cutoff) + 1) if derived_cutoff is not None else None
    for index, page in enumerate(pages):
        try:
            response_envelope, sources = _validated_page_response(
                page.get("responseEnvelope"),
                channel_id=identity["channelId"],
                before=expected_before,
                limit=page_limit,
            )
        except RichArchiveError:
            response_envelope = {}
            sources = []
            errors.append(f"pagination_page_invalid:{index}")
        page_ids = [str(row.get("id") or "") for row in sources]
        page_terminal = not sources
        if (
            page.get("pageIndex") != index
            or page.get("requestBefore") != expected_before
            or page.get("requestLimit") != page_limit
            or page.get("responseEnvelope") != response_envelope
            or page.get("responseEnvelopeSha256") != json_sha256(response_envelope)
            or page.get("terminal") is not page_terminal
            or any(not value.isdigit() for value in page_ids)
            or len(page_ids) != len(set(page_ids))
            or page_ids != sorted(page_ids, key=int, reverse=True)
            or any(value in seen_page_ids for value in page_ids)
            or (
                expected_before is not None
                and any(int(value) >= int(expected_before) for value in page_ids if value.isdigit())
            )
            or (derived_cutoff is None and bool(page_ids))
            or (
                derived_cutoff is not None
                and any(int(value) > int(derived_cutoff) for value in page_ids if value.isdigit())
            )
            or (index < len(pages) - 1 and page_terminal)
            or (index == len(pages) - 1 and not page_terminal)
        ):
            errors.append(f"pagination_page_invalid:{index}")
        seen_page_ids.update(page_ids)
        page_sources.extend(sources)
        if page_ids:
            expected_before = page_ids[-1]

    try:
        derived_rows = sorted(
            (
                _active_live_binding(normalize_message(
                    source,
                    expected_channel_id=identity["channelId"],
                    observed_at=completed,
                ))
                for source in page_sources
            ),
            key=lambda row: int(row["messageId"]),
        )
        for source in cutoff_sources:
            normalize_message(
                source,
                expected_channel_id=identity["channelId"],
                observed_at=completed,
            )
    except RichArchiveError:
        derived_rows = []
        errors.append("pagination_source_payload_invalid")
    if message_rows != derived_rows:
        errors.append("live_messages_not_derived_from_pagination")
    if enumeration.get("fetchedMessageCount") != len(derived_rows):
        errors.append("live_page_denominator_mismatch")

    ids = [str(row.get("messageId") or "") for row in message_rows]
    if any(not value.isdigit() for value in ids) or len(ids) != len(set(ids)) or ids != sorted(ids, key=int):
        errors.append("live_message_identity_set_invalid")
    records = projection["records"]
    if set(ids) != set(records):
        errors.append("live_and_canonical_id_sets_differ")
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
        "runContextId": str(transaction.get("runContextId") or ""),
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


def _build_runtime_pass_receipt(root: Path, evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute a runtime receipt from collector evidence and staged bytes."""
    projection = _generation_projection(root)
    validated, evidence_errors = _validated_live_evidence(root, evidence, projection)
    counts = validated["counts"]
    inventory = generation_inventory(root)
    errors = list(evidence_errors)
    try:
        asset_reservation = _validated_persisted_asset_reservation(
            root,
            validated["identity"],
        )
    except (RichArchiveError, OSError, ValueError):
        asset_reservation = {}
        errors.append("full_run_asset_reservation_invalid")
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
        "runContextId": validated["runContextId"],
        "inventoryDigest": validated["inventory"].get("digest"),
        "verifiedCutoff": validated["cutoff"],
        "liveEvidenceSha256": validated["evidenceSha256"],
        "immutableEvidenceSha256": json_sha256(validated["immutable"]),
        "assetReservationId": asset_reservation.get("reservationId"),
        "assetReservationSha256": asset_reservation.get("receiptSha256"),
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
        "runContextId": validated["runContextId"],
        "liveEvidencePath": "receipts/live-inventory-evidence.json",
        "liveEvidenceSha256": validated["evidenceSha256"],
        "immutableEvidenceVerified": "PASS",
        "immutableEvidenceSha256": json_sha256(validated["immutable"]),
        "assetReservationId": asset_reservation.get("reservationId"),
        "assetReservationSha256": asset_reservation.get("receiptSha256"),
        "contentGenerationSha256": inventory["contentGenerationSha256"],
        "fullGateBindingSha256": json_sha256(binding),
        "errors": sorted(set(errors)),
    }
    return receipt


def _persisted_audit_receipt(runtime_receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Persist evidence for audit without turning stored bytes into PASS authority."""
    if runtime_receipt.get("gateStatus") != "PASS":
        raise GenerationError("only a runtime PASS may be recorded as audit evidence")
    receipt = dict(runtime_receipt)
    receipt["gateStatus"] = "AUDIT_ONLY"
    receipt["runtimeVerificationRequired"] = True
    receipt["runtimePassReceiptSha256"] = json_sha256(runtime_receipt)
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
            runtime_receipt = _build_runtime_pass_receipt(root, evidence)
            expected_receipt = _persisted_audit_receipt(runtime_receipt)
        except RichArchiveError as exc:
            full_gate_errors.append(f"live_evidence_invalid:{type(exc).__name__}")
        else:
            full_gate_errors.extend(runtime_receipt.get("errors") or [])
            if receipt != expected_receipt:
                full_gate_errors.append("receipt_does_not_equal_recomputed_audit_evidence")
            if runtime_receipt.get("gateStatus") != "PASS":
                full_gate_errors.append("recomputed_runtime_gate_not_pass")
    else:
        full_gate_errors.append("concrete_live_evidence_or_v2_receipt_missing")
    if receipt.get("gateStatus") == "PASS":
        raise GenerationError("stored receipt may not self-assert live PASS")
    if require_full_gate:
        raise GenerationError(
            "offline verification cannot prove live completeness; runtime live evidence token is required"
        )
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


def _verify_full_generation_runtime(
    root: Path,
    *,
    live_evidence_token: LiveEvidenceToken | None,
    allowed_states: set[str],
) -> dict[str, Any]:
    """Return PASS only while a registered collector capability is live."""
    registration = _require_live_evidence_token(
        live_evidence_token,
        root=root,
        allowed_states=allowed_states,
    )
    evidence = json.loads(json.dumps(registration["evidence"], ensure_ascii=False))
    identity = _validated_entry_identity(evidence.get("entryIdentity"))
    run_context = registration["runContext"]()
    run_registration = _require_run_context(
        run_context,
        kind="full_rebuild",
        identity=identity,
        entry_root=registration["entryRoot"],
    )
    asset_reservation = _validated_stage_asset_reservation(
        root,
        run_registration,
        identity,
        require_unused=True,
    )
    local = verify_generation(root)
    evidence_path = _lexical_absolute(root) / "receipts" / "live-inventory-evidence.json"
    receipt_path = _lexical_absolute(root) / "receipts" / "rich-archive-latest.json"
    if not _regular_single_link(evidence_path) or not _regular_single_link(receipt_path):
        raise GenerationError("runtime full gate audit evidence is missing")
    persisted_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if persisted_evidence != evidence:
        raise GenerationError("runtime token does not match persisted live evidence")
    runtime_receipt = _build_runtime_pass_receipt(root, evidence)
    if runtime_receipt.get("gateStatus") != "PASS" or runtime_receipt.get("errors"):
        raise GenerationError("runtime live completeness gate is not PASS")
    if (
        runtime_receipt.get("assetReservationId") != asset_reservation.get("reservationId")
        or runtime_receipt.get("assetReservationSha256") != asset_reservation.get("receiptSha256")
    ):
        raise GenerationError("runtime receipt does not match the full-run asset reservation")
    persisted_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if persisted_receipt != _persisted_audit_receipt(runtime_receipt):
        raise GenerationError("persisted audit receipt does not match runtime verification")
    if not local.get("ok") or local.get("fullGateErrors"):
        raise GenerationError("local generation or audit evidence verification failed")
    return {
        **local,
        "gateStatus": "PASS",
        "fullGatePresent": True,
        "runtimeEvidenceVerified": True,
        "runtimeReceipt": runtime_receipt,
    }


def verify_full_generation(
    root: Path,
    *,
    live_evidence_token: LiveEvidenceToken | None,
) -> dict[str, Any]:
    return _verify_full_generation_runtime(
        root,
        live_evidence_token=live_evidence_token,
        allowed_states={"fresh", "prepared"},
    )


_LOCK_TOKEN_GUARD = object()
_LOCK_TOKEN_REGISTRY: dict[str, dict[str, Any]] = {}
_LOCK_TOKEN_REGISTRY_MUTEX = threading.RLock()


def _validated_lock_token_identity(
    lock_token: ArchiveLockToken | None,
    *,
    allow_closing_owner: bool = False,
) -> dict[str, Any]:
    """Return immutable identity only for a module-issued, currently held lock."""
    with _LOCK_TOKEN_REGISTRY_MUTEX:
        registration = (
            _LOCK_TOKEN_REGISTRY.get(lock_token._nonce)
            if isinstance(lock_token, ArchiveLockToken)
            else None
        )
        owner_count = (
            registration.get("borrowOwners", {}).get(threading.get_ident(), 0)
            if registration is not None else 0
        )
        if (
            not isinstance(lock_token, ArchiveLockToken)
            or lock_token.closed
            or lock_token._pid != os.getpid()
            or registration is None
            or registration.get("token")() is not lock_token
            or registration.get("pid") != os.getpid()
            or (
                registration.get("closing")
                and not (allow_closing_owner and owner_count > 0)
            )
        ):
            raise RichArchiveError("valid held backup lock token is required to begin a run")
        path = registration.get("path")
        handle = registration.get("handle")
        if not isinstance(path, Path):
            raise RichArchiveError("backup lock token path identity is invalid")
        try:
            descriptor_info = os.fstat(handle.fileno())
            path_info = path.lstat()
        except (AttributeError, OSError, ValueError) as exc:
            raise RichArchiveError("backup lock token is no longer held") from exc
        if (
            descriptor_info.st_dev != registration.get("device")
            or descriptor_info.st_ino != registration.get("inode")
            or path_info.st_dev != registration.get("device")
            or path_info.st_ino != registration.get("inode")
            or not stat.S_ISREG(path_info.st_mode)
            or path_info.st_nlink != 1
            or handle.fileno() != registration.get("descriptor")
        ):
            raise RichArchiveError("backup lock token identity no longer matches its file")
        probe_descriptor = os.open(
            path,
            os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            try:
                fcntl.flock(probe_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise RichArchiveError("backup lock ownership probe failed") from exc
            else:
                fcntl.flock(probe_descriptor, fcntl.LOCK_UN)
                raise RichArchiveError("backup lock token does not hold its lock")
        finally:
            os.close(probe_descriptor)
        return {
            "nonce": lock_token._nonce,
            "path": path,
            "device": descriptor_info.st_dev,
            "inode": descriptor_info.st_ino,
        }


def _pin_lock_token_for_run(lock_token: ArchiveLockToken) -> dict[str, Any]:
    """Create a non-thread-affine pin that keeps the canonical flock held."""
    with _LOCK_TOKEN_REGISTRY_MUTEX:
        _validated_lock_token_identity(lock_token)
        registration = _LOCK_TOKEN_REGISTRY.get(lock_token._nonce)
        if (
            registration is None
            or registration.get("token")() is not lock_token
            or registration.get("closing")
            or registration.get("closeRequested")
        ):
            raise RichArchiveError("backup lock cannot be pinned for a new archive run")
        pin_nonce = secrets.token_hex(32)
        registration["runPins"].add(pin_nonce)
        return {
            "pinNonce": pin_nonce,
            "tokenNonce": lock_token._nonce,
            "registration": registration,
            "token": lock_token,
        }


def _release_run_lock_pin(
    pin: Mapping[str, Any],
    *,
    request_close: bool = True,
) -> None:
    """Drop one run pin; a close request releases flock only after the last pin."""
    with _LOCK_TOKEN_REGISTRY_MUTEX:
        token = pin["token"]
        token_nonce = str(pin["tokenNonce"])
        registration = _LOCK_TOKEN_REGISTRY.get(token_nonce)
        if (
            registration is not pin["registration"]
            or registration.get("token")() is not token
            or pin["pinNonce"] not in registration.get("runPins", set())
        ):
            raise RichArchiveError("archive run lock pin registry changed during release")
        registration["runPins"].remove(pin["pinNonce"])
        if request_close:
            registration["closeRequested"] = True
        if not registration["runPins"] and registration.get("closeRequested"):
            if registration.get("borrowCount", 0) > 0:
                registration["closing"] = True
            else:
                _release_lock_registration(token_nonce, expected_token=token)


def _active_run_registration_for_entry(entry_root: Path) -> dict[str, Any] | None:
    entry_key = str(_lexical_absolute(entry_root))
    with _RUN_CONTEXT_REGISTRY_MUTEX:
        nonce = _ACTIVE_ENTRY_ROOT_RUNS.get(entry_key)
        if nonce is None:
            return None
        registration = _RUN_CONTEXT_REGISTRY.get(nonce)
        if registration is None:
            _ACTIVE_ENTRY_ROOT_RUNS.pop(entry_key, None)
            return None
        if registration.get("context")() is None:
            lock_run_pin = _remove_run_context_registration_locked(nonce)
            if lock_run_pin is not None:
                _release_run_lock_pin(lock_run_pin)
            return None
        return registration


def _require_active_run_entry_lock(
    entry_root: Path,
    lock_token: ArchiveLockToken | None,
) -> dict[str, Any] | None:
    registration = _active_run_registration_for_entry(entry_root)
    if registration is None:
        return None
    if lock_token is not registration["lockToken"]:
        raise RichArchiveError("active archive run requires its bound lock token")
    owner_count = registration.get("borrowOwners", {}).get(threading.get_ident(), 0)
    if registration.get("closing") and owner_count == 0:
        raise RichArchiveError("active archive run is closing")
    identity = _validated_lock_token_identity(
        lock_token,
        allow_closing_owner=True,
    )
    if (
        identity["nonce"] != registration["lockTokenNonce"]
        or identity["path"] != registration["lockPath"]
        or identity["device"] != registration["lockDevice"]
        or identity["inode"] != registration["lockInode"]
    ):
        raise RichArchiveError("active archive run lock identity changed")
    return registration


def _begin_active_run_entry_lease(
    entry_root: Path,
    lock_token: ArchiveLockToken | None,
) -> dict[str, Any] | None:
    """Keep an active run registered until one entry operation fully exits."""
    entry_key = str(_lexical_absolute(entry_root))
    owner = threading.get_ident()
    with _RUN_CONTEXT_REGISTRY_MUTEX:
        nonce = _ACTIVE_ENTRY_ROOT_RUNS.get(entry_key)
        if nonce is None:
            return None
        registration = _RUN_CONTEXT_REGISTRY.get(nonce)
        context = registration.get("context")() if registration is not None else None
        if registration is None or context is None:
            lock_run_pin = _remove_run_context_registration_locked(nonce)
            if lock_run_pin is not None:
                _release_run_lock_pin(lock_run_pin)
            return None
        _require_active_run_entry_lock(entry_root, lock_token)
        owners = registration["borrowOwners"]
        if registration.get("closing") and owners.get(owner, 0) == 0:
            raise RichArchiveError("active archive run is closing")
        owners[owner] = owners.get(owner, 0) + 1
        registration["borrowCount"] += 1
        return {
            "nonce": nonce,
            "owner": owner,
            "registration": registration,
            "context": context,
        }


def _end_active_run_entry_lease(lease: Mapping[str, Any] | None) -> None:
    if lease is None:
        return
    lock_run_pin: dict[str, Any] | None = None
    with _RUN_CONTEXT_REGISTRY_MUTEX:
        registration = _RUN_CONTEXT_REGISTRY.get(str(lease["nonce"]))
        if registration is not lease["registration"]:
            raise RichArchiveError("archive run lease registry changed during operation")
        owners = registration["borrowOwners"]
        owner = int(lease["owner"])
        if owners.get(owner, 0) < 1 or registration.get("borrowCount", 0) < 1:
            raise RichArchiveError("archive run lease count underflow")
        owners[owner] -= 1
        if owners[owner] == 0:
            owners.pop(owner)
        registration["borrowCount"] -= 1
        if registration["borrowCount"] == 0 and registration.get("closing"):
            lock_run_pin = _remove_run_context_registration_locked(str(lease["nonce"]))
    if lock_run_pin is not None:
        _release_run_lock_pin(lock_run_pin)


@contextmanager
def _borrow_active_run_entry(
    entry_root: Path,
    lock_token: ArchiveLockToken | None,
) -> Iterator[None]:
    lease = _begin_active_run_entry_lease(entry_root, lock_token)
    try:
        yield
    finally:
        _end_active_run_entry_lease(lease)


class ArchiveLockToken:
    """Opaque capability; the locked descriptor exists only in the registry."""

    __slots__ = ("_nonce", "_pid", "_closed", "__weakref__")

    def __init__(self, *, guard: object) -> None:
        if guard is not _LOCK_TOKEN_GUARD:
            raise RichArchiveError("archive lock token cannot be constructed externally")
        self._pid = os.getpid()
        self._nonce = secrets.token_hex(32)
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        if _release_lock_registration(self._nonce, expected_token=self):
            self._closed = True

    def __enter__(self) -> "ArchiveLockToken":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


def _release_lock_registration(
    nonce: str,
    *,
    expected_token: ArchiveLockToken | None = None,
) -> bool:
    with _LOCK_TOKEN_REGISTRY_MUTEX:
        registration = _LOCK_TOKEN_REGISTRY.get(nonce)
        if registration is None:
            return True
        token = registration.get("token")()
        if expected_token is not None and token is not expected_token:
            return False
        registration["closeRequested"] = True
        if registration.get("runPins"):
            return False
        if registration.get("borrowCount", 0) > 0:
            registration["closing"] = True
            return False
        _LOCK_TOKEN_REGISTRY.pop(nonce, None)
        if token is not None:
            token._closed = True
        handle = registration.get("handle")
        if handle is None:
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            try:
                handle.close()
            except (OSError, ValueError):
                pass
        return True


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
        active_run = _active_run_registration_for_entry(self.entry_root)
        if active_run is not None and self.lock_path != active_run["lockPath"]:
            raise RichArchiveError("active archive run forbids an alternate backup lock path")
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
        token = ArchiveLockToken(guard=_LOCK_TOKEN_GUARD)
        token_nonce = token._nonce
        token_ref = weakref.ref(
            token,
            lambda _reference, nonce=token_nonce: _release_lock_registration(nonce),
        )
        with _LOCK_TOKEN_REGISTRY_MUTEX:
            _LOCK_TOKEN_REGISTRY[token._nonce] = {
                "token": token_ref,
                "pid": os.getpid(),
                "path": self.lock_path,
                "device": info.st_dev,
                "inode": info.st_ino,
                "descriptor": handle.fileno(),
                "handle": handle,
                "borrowCount": 0,
                "borrowOwners": {},
                "runPins": set(),
                "closeRequested": False,
                "closing": False,
            }
        return token

    def _lock_registration(
        self,
        lock_token: ArchiveLockToken | None,
        *,
        allow_closing_owner: bool = False,
    ) -> dict[str, Any]:
        if self.lock_path is None:
            raise RichArchiveError("shared backup lock path is required for archive mutation")
        registration = (
            _LOCK_TOKEN_REGISTRY.get(lock_token._nonce)
            if isinstance(lock_token, ArchiveLockToken)
            else None
        )
        if (
            not isinstance(lock_token, ArchiveLockToken)
            or lock_token.closed
            or lock_token._pid != os.getpid()
            or registration is None
            or registration.get("token")() is not lock_token
            or registration.get("pid") != os.getpid()
            or registration.get("path") != self.lock_path
        ):
            raise RichArchiveError("valid shared backup lock ownership token is required")
        owner_count = registration.get("borrowOwners", {}).get(threading.get_ident(), 0)
        if registration.get("closing") and not (allow_closing_owner and owner_count > 0):
            raise RichArchiveError("shared backup lock is closing")
        handle = registration.get("handle")
        try:
            descriptor_info = os.fstat(handle.fileno())
            path_info = self.lock_path.lstat()
        except (AttributeError, OSError, ValueError) as exc:
            raise RichArchiveError("shared backup lock ownership token is no longer valid") from exc
        if (
            descriptor_info.st_dev != registration.get("device")
            or descriptor_info.st_ino != registration.get("inode")
            or path_info.st_dev != registration.get("device")
            or path_info.st_ino != registration.get("inode")
            or not stat.S_ISREG(path_info.st_mode)
            or path_info.st_nlink != 1
            or handle.fileno() != registration.get("descriptor")
        ):
            raise RichArchiveError("shared backup lock ownership token no longer matches lock file")
        # On supported flock platforms, a second open-file description must be
        # unable to obtain the same exclusive lock while this token is live.
        probe_descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            try:
                fcntl.flock(probe_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise RichArchiveError("shared backup lock ownership probe failed") from exc
            else:
                fcntl.flock(probe_descriptor, fcntl.LOCK_UN)
                raise RichArchiveError("shared backup lock is not actually held")
        finally:
            os.close(probe_descriptor)
        return registration

    def _require_lock(self, lock_token: ArchiveLockToken | None) -> ArchiveLockToken:
        with _LOCK_TOKEN_REGISTRY_MUTEX:
            self._lock_registration(lock_token, allow_closing_owner=True)
        assert isinstance(lock_token, ArchiveLockToken)
        return lock_token

    @contextmanager
    def _borrow_lock(self, lock_token: ArchiveLockToken | None) -> Iterator[ArchiveLockToken]:
        """Hold the descriptor until the complete mutation (including nested calls) exits."""
        lease = self._begin_lock_lease(lock_token)
        assert isinstance(lock_token, ArchiveLockToken)
        try:
            yield lock_token
        finally:
            self._end_lock_lease(lease)

    def _begin_lock_lease(self, lock_token: ArchiveLockToken | None) -> dict[str, Any]:
        owner = threading.get_ident()
        with _LOCK_TOKEN_REGISTRY_MUTEX:
            registration = self._lock_registration(
                lock_token,
                allow_closing_owner=True,
            )
            owners = registration["borrowOwners"]
            if any(
                thread_id != owner and count > 0
                for thread_id, count in owners.items()
            ):
                raise RichArchiveError(
                    "shared backup lock token is already in use by another thread"
                )
            if registration.get("closing") and owners.get(owner, 0) == 0:
                raise RichArchiveError("shared backup lock is closing")
            owners[owner] = owners.get(owner, 0) + 1
            registration["borrowCount"] += 1
        assert isinstance(lock_token, ArchiveLockToken)
        return {
            "nonce": lock_token._nonce,
            "owner": owner,
            "registration": registration,
            "token": lock_token,
        }

    def _end_lock_lease(self, lease: Mapping[str, Any]) -> None:
        lock_token = lease["token"]
        owner = lease["owner"]
        registration = lease["registration"]
        with _LOCK_TOKEN_REGISTRY_MUTEX:
            current = _LOCK_TOKEN_REGISTRY.get(lease["nonce"])
            if current is not registration or current.get("token")() is not lock_token:
                raise RichArchiveError("shared backup lock lease registry changed during mutation")
            owners = current["borrowOwners"]
            if owners.get(owner, 0) < 1 or current.get("borrowCount", 0) < 1:
                raise RichArchiveError("shared backup lock lease count underflow")
            owners[owner] -= 1
            if owners[owner] == 0:
                owners.pop(owner)
            current["borrowCount"] -= 1
            if (
                current["borrowCount"] == 0
                and not current.get("runPins")
                and current.get("closeRequested")
            ):
                _release_lock_registration(lock_token._nonce, expected_token=lock_token)

    def _require_active_lock_lease(self, lock_token: ArchiveLockToken | None) -> None:
        with _LOCK_TOKEN_REGISTRY_MUTEX:
            registration = self._lock_registration(
                lock_token,
                allow_closing_owner=True,
            )
            if registration.get("borrowOwners", {}).get(threading.get_ident(), 0) < 1:
                raise RichArchiveError("active shared backup lock operation lease is required")

    def _managed_run_registration(
        self,
        run_context: ArchiveRunContext | None,
        lock_token: ArchiveLockToken | None,
    ) -> tuple[dict[str, Any], ArchiveLockToken]:
        """Bind one public managed mutation to its module-minted run and lock."""
        registration = _require_run_context(
            run_context,
            entry_root=self.entry_root,
        )
        bound_token = registration["lockToken"]
        if lock_token is None:
            lock_token = bound_token
        registration = _require_run_context(
            run_context,
            entry_root=self.entry_root,
            lock_token=lock_token,
        )
        if self.lock_path != registration["lockPath"]:
            raise RichArchiveError(
                "managed archive mutation requires the core-derived canonical backup lock path"
            )
        assert isinstance(lock_token, ArchiveLockToken)
        return registration, lock_token

    def _pointer_body(self, generation_id: str, generation_sha256: str) -> dict[str, str]:
        return {
            "schemaVersion": POINTER_SCHEMA,
            "generationId": generation_id,
            "generationSha256": generation_sha256,
        }

    def _current_pointer_snapshot(self) -> dict[str, Any]:
        """Read and verify one stable CURRENT state for stage CAS binding."""
        reject_symlink_path(self.entry_root)
        reject_symlink_path(self.pointer_path)
        if not self.pointer_path.exists():
            current = self.resolve_current()
            if current is not None or self.pointer_path.exists():
                raise GenerationError("CURRENT changed while its base state was captured")
            return {
                "basePointerPresent": False,
                "basePointerSha256": None,
                "baseGenerationId": None,
                "baseGenerationSha256": None,
                "currentPath": None,
            }
        if not _regular_single_link(self.pointer_path):
            raise GenerationError("CURRENT pointer is not a regular file")
        before = self.pointer_path.read_bytes()
        try:
            pointer = json.loads(before.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GenerationError("CURRENT pointer is not valid JSON") from exc
        if not isinstance(pointer, Mapping):
            raise GenerationError("CURRENT pointer payload is invalid")
        pointer_body = dict(pointer)
        checksum = pointer_body.pop("pointerSha256", None)
        if checksum != json_sha256(pointer_body) or pointer_body.get("schemaVersion") != POINTER_SCHEMA:
            raise GenerationError("CURRENT pointer checksum or schema mismatch")
        generation_id = _validated_generation_id(pointer_body.get("generationId"))
        generation_sha256 = str(pointer_body.get("generationSha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", generation_sha256):
            raise GenerationError("CURRENT pointer generation checksum is invalid")
        current = self.resolve_current()
        if current is None or current.name != generation_id:
            raise GenerationError("CURRENT pointer changed while its base state was captured")
        after = self.pointer_path.read_bytes()
        if before != after:
            raise GenerationError("CURRENT changed while its base state was captured")
        return {
            "basePointerPresent": True,
            "basePointerSha256": hashlib.sha256(before).hexdigest(),
            "baseGenerationId": generation_id,
            "baseGenerationSha256": generation_sha256,
            "currentPath": current,
        }

    def _stage_base_receipt(
        self,
        stage: Path,
        generation_id: str,
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "schemaVersion": STAGE_BASE_SCHEMA,
            "entryRootSha256": json_sha256(str(self.entry_root)),
            "generationId": generation_id,
            "basePointerPresent": snapshot["basePointerPresent"],
            "basePointerSha256": snapshot["basePointerSha256"],
            "baseGenerationId": snapshot["baseGenerationId"],
            "baseGenerationSha256": snapshot["baseGenerationSha256"],
        }
        receipt["receiptSha256"] = json_sha256(receipt)
        return receipt

    def _validated_stage_base_receipt(self, stage: Path) -> dict[str, Any]:
        stage = _lexical_absolute(stage)
        entry_root, generation_id = _generation_binding(stage)
        if entry_root != self.entry_root:
            raise GenerationError("stage base receipt targets another archive entry")
        path = contained_path(stage, "receipts/stage-base-current.json")
        if not _regular_single_link(path):
            raise GenerationError("stage base CURRENT receipt is missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GenerationError("stage base CURRENT receipt is invalid") from exc
        if not isinstance(value, Mapping):
            raise GenerationError("stage base CURRENT receipt payload is invalid")
        receipt = dict(value)
        checksum = receipt.pop("receiptSha256", None)
        if checksum != json_sha256(receipt):
            raise GenerationError("stage base CURRENT receipt checksum mismatch")
        present = receipt.get("basePointerPresent")
        pointer_sha = receipt.get("basePointerSha256")
        base_generation_id = receipt.get("baseGenerationId")
        base_generation_sha = receipt.get("baseGenerationSha256")
        if (
            receipt.get("schemaVersion") != STAGE_BASE_SCHEMA
            or receipt.get("entryRootSha256") != json_sha256(str(entry_root))
            or receipt.get("generationId") != generation_id
            or not isinstance(present, bool)
            or (
                present
                and (
                    not re.fullmatch(r"[0-9a-f]{64}", str(pointer_sha or ""))
                    or _validated_generation_id(base_generation_id) != base_generation_id
                    or not re.fullmatch(r"[0-9a-f]{64}", str(base_generation_sha or ""))
                )
            )
            or (
                not present
                and any(value is not None for value in (
                    pointer_sha, base_generation_id, base_generation_sha,
                ))
            )
        ):
            raise GenerationError("stage base CURRENT receipt binding is invalid")
        return dict(value)

    def _require_stage_base_current_unchanged(self, stage: Path) -> None:
        receipt = self._validated_stage_base_receipt(stage)
        current = self._current_pointer_snapshot()
        for field_name in (
            "basePointerPresent",
            "basePointerSha256",
            "baseGenerationId",
            "baseGenerationSha256",
        ):
            if receipt.get(field_name) != current.get(field_name):
                raise GenerationError("CURRENT changed since stage creation; stale stage rejected")

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
        run_context: ArchiveRunContext | None = None,
    ) -> Path:
        _, lock_token = self._managed_run_registration(run_context, lock_token)
        run_lease = _begin_active_run_entry_lease(self.entry_root, lock_token)
        try:
            with self._borrow_lock(lock_token):
                _require_active_run_entry_lock(self.entry_root, lock_token)
                return self._create_stage_under_lease(
                    generation_id,
                    copy_current=copy_current,
                    lock_token=lock_token,
                )
        finally:
            _end_active_run_entry_lease(run_lease)

    def _create_stage_under_lease(
        self,
        generation_id: str,
        *,
        copy_current: bool,
        lock_token: ArchiveLockToken | None,
    ) -> Path:
        self._require_active_lock_lease(lock_token)
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
            self._validated_stage_base_receipt(stage)
            return stage
        snapshot = self._current_pointer_snapshot()
        current = snapshot["currentPath"] if copy_current else None
        if current is not None:
            verify_generation(current)
            shutil.copytree(current, stage, copy_function=shutil.copy2, symlinks=True)
            (stage / "generation-manifest.json").unlink(missing_ok=True)
        else:
            stage.mkdir(mode=0o700)
        for name in ("canonical", "raw", "attachments", "receipts", "legacy-retained"):
            (stage / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(
            contained_path(stage, "receipts/stage-base-current.json"),
            self._stage_base_receipt(stage, generation_id, snapshot),
        )
        return stage

    def merge_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        channel_id: str,
        relative_path: str,
        observed_at: str,
        generation_id: str,
        downloader: AssetDownloader | None = None,
        lock_token: ArchiveLockToken | None = None,
        run_context: ArchiveRunContext,
    ) -> dict[str, Any]:
        identity = _validated_entry_identity({
            "channelId": str(channel_id),
            "relativePath": relative_path,
            "normalizedRelativePath": unicodedata.normalize("NFKC", relative_path).casefold(),
        })
        run_registration = _require_run_context(
            run_context,
            identity=identity,
            entry_root=self.entry_root,
        )
        if lock_token is None:
            lock_token = run_registration["lockToken"]
        run_registration = _require_run_context(
            run_context,
            identity=identity,
            entry_root=self.entry_root,
            lock_token=lock_token,
        )
        run_lease = _begin_active_run_entry_lease(self.entry_root, lock_token)
        lease: dict[str, Any] | None = None
        budget_reservation: tuple[AssetRunBudget, dict[str, int]] | None = None
        published = False
        try:
            lease = self._begin_lock_lease(lock_token)
            run_registration = _require_run_context(
                run_context,
                identity=identity,
                entry_root=self.entry_root,
                lock_token=lock_token,
            )
            _require_active_run_entry_lock(self.entry_root, lock_token)
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
                budget = run_registration["budget"]
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
                exact_capacity = budget.reserve_entry(
                    assets,
                    self.entry_root,
                    assume_unknown_max=False,
                )
                budget_reservation = (budget, exact_capacity)
            stage = self.create_stage(
                generation_id,
                copy_current=True,
                lock_token=lock_token,
                run_context=run_context,
            )
            self._require_active_lock_lease(lock_token)
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
                stage,
                generation_id,
                manifest["generationSha256"],
                lock_token=lock_token,
                run_context=run_context,
            )
            published = True
            return {"generationId": generation_id, "verified": True, **local}
        finally:
            if budget_reservation is not None and not published:
                keep_reservation = True
                try:
                    current_after_failure = self.resolve_current()
                except (OSError, ValueError, RichArchiveError):
                    pass
                else:
                    keep_reservation = (
                        current_after_failure is not None
                        and current_after_failure.name == generation_id
                    )
                if not keep_reservation:
                    budget_reservation[0].release_entry(budget_reservation[1])
            if lease is not None:
                self._end_lock_lease(lease)
            _end_active_run_entry_lease(run_lease)

    def materialize_full_stage_from_live_evidence(
        self,
        *,
        generation_id: str,
        live_evidence_token: LiveEvidenceToken | None,
        downloader: AssetDownloader,
        lock_token: ArchiveLockToken | None = None,
    ) -> Path:
        """Write the exact process-local live snapshot into a private stage.

        The collector, normalizer, stage writer, and asset budget all remain in
        one rich-core authority boundary.  No CURRENT pointer is changed here.
        """
        registration = _require_live_evidence_token(
            live_evidence_token,
            allowed_states={"fresh"},
        )
        run_context = registration["runContext"]()
        run_registration = _require_run_context(
            run_context,
            kind="full_rebuild",
            entry_root=self.entry_root,
        )
        if lock_token is None:
            lock_token = run_registration["lockToken"]
        _require_run_context(
            run_context,
            kind="full_rebuild",
            entry_root=self.entry_root,
            lock_token=lock_token,
        )
        generation_id = _validated_generation_id(generation_id)
        records_value = registration.get("normalizedRecords")
        if not isinstance(records_value, list) or any(
            not isinstance(row, Mapping) for row in records_value
        ):
            raise GenerationError("runtime live evidence records are unavailable")
        records = json.loads(json.dumps(records_value, ensure_ascii=False))
        by_day: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            created = _iso_timestamp(
                record.get("createdTimestamp"),
                field="createdTimestamp",
                required=True,
            )
            assert created is not None
            day = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(
                TZ_TAIPEI
            ).date().isoformat()
            by_day.setdefault(day, []).append(record)

        budget = run_registration["budget"]
        if budget.limits != downloader.limits:
            raise AssetDownloadError(
                "shared asset run budget limits do not match downloader limits"
            )
        assets = [
            asset
            for rows in by_day.values()
            for record in rows
            for observation in record["observations"]
            for asset in observation["assetInventory"]
        ]
        budget.preflight_entry(
            assets,
            self.entry_root,
            assume_unknown_max=True,
        )
        unknown_count = sum(
            1
            for asset in assets
            if asset.get("inScope") and asset.get("declaredSize") is None
        )
        budget.probe_budget.ensure_capacity(unknown_count)
        by_day = {
            day: [
                resolve_asset_sizes(
                    record,
                    downloader,
                    probe_budget=budget.probe_budget,
                )
                for record in rows
            ]
            for day, rows in by_day.items()
        }
        assets = [
            asset
            for rows in by_day.values()
            for record in rows
            for observation in record["observations"]
            for asset in observation["assetInventory"]
        ]
        budget.preflight_entry(
            assets,
            self.entry_root,
            assume_unknown_max=False,
        )
        stage = self.create_stage(
            generation_id,
            copy_current=False,
            lock_token=lock_token,
            run_context=run_context,
        )
        by_day = {
            day: [apply_asset_results(record, downloader, stage) for record in rows]
            for day, rows in by_day.items()
        }
        for day, rows in by_day.items():
            rows.sort(key=lambda row: int(str(row["messageId"])))
            atomic_jsonl(stage / "canonical" / f"{day}.jsonl", rows)
            _atomic_bytes(
                stage / "raw" / f"{day}.md",
                render_day(rows).encode("utf-8"),
            )
        return stage

    def rebind_materialized_full_stage_from_live_evidence(
        self,
        stage: Path,
        *,
        live_evidence_token: LiveEvidenceToken | None,
        lock_token: ArchiveLockToken | None = None,
    ) -> dict[str, Any]:
        """Rebind an unsealed, fully materialized stage to fresh Discord proof.

        This is intentionally narrower than a normal resume.  It accepts only
        a stage that stopped after all canonical/raw/attachment bytes were
        written but before any reservation, PASS receipt, or manifest existed.
        Fresh records may differ from staged records only by verified Discord
        CDN signature query churn; completed local attachment receipts are
        grafted onto the fresh source records after their bytes are rehashed.
        """
        initial = _require_live_evidence_token(
            live_evidence_token,
            allowed_states={"fresh"},
        )
        run_context = initial["runContext"]()
        run_registration = _require_run_context(
            run_context,
            kind="full_rebuild",
            entry_root=self.entry_root,
        )
        if lock_token is None:
            lock_token = run_registration["lockToken"]
        _require_run_context(
            run_context,
            kind="full_rebuild",
            entry_root=self.entry_root,
            lock_token=lock_token,
        )
        run_lease = _begin_active_run_entry_lease(self.entry_root, lock_token)
        try:
            with self._borrow_lock(lock_token):
                stage = _lexical_absolute(stage)
                registration = _require_live_evidence_token(
                    live_evidence_token,
                    root=stage,
                    allowed_states={"fresh"},
                )
                entry_root, generation_id = _generation_binding(stage)
                expected_stage = contained_path(self.staging, generation_id)
                if (
                    entry_root != self.entry_root
                    or stage != expected_stage
                    or stage.is_symlink()
                    or not stage.is_dir()
                ):
                    raise GenerationError("materialized resume stage path is invalid")
                self._require_stage_base_current_unchanged(stage)

                receipts_root = contained_path(stage, "receipts")
                if receipts_root.is_symlink() or not receipts_root.is_dir():
                    raise GenerationError("materialized resume receipt directory is invalid")
                receipt_files = {
                    path.name
                    for path in receipts_root.iterdir()
                    if path.is_file() or path.is_symlink()
                }
                if receipt_files != {"stage-base-current.json"} or any(
                    not _regular_single_link(path)
                    for path in receipts_root.iterdir()
                ):
                    raise GenerationError("materialized resume stage has advanced receipts")
                if os.path.lexists(stage / "generation-manifest.json"):
                    raise GenerationError("materialized resume stage is already manifested")

                projection = _generation_projection(stage)
                if any(
                    int(projection[field]) != 0
                    for field in (
                        "duplicateCanonicalIds",
                        "unknownVisibleFields",
                        "attachmentErrors",
                        "sectionCoverageErrors",
                        "markdownErrors",
                    )
                ) or projection["binaryExpected"] != projection["binaryVerified"]:
                    raise GenerationError("materialized resume stage failed local verification")
                _verified_generation_assets(stage)

                fresh_value = registration.get("normalizedRecords")
                if not isinstance(fresh_value, list) or any(
                    not isinstance(row, Mapping) for row in fresh_value
                ):
                    raise GenerationError("materialized resume fresh records are missing")
                fresh_records = json.loads(json.dumps(fresh_value, ensure_ascii=False))
                old_records = projection["records"]
                if {str(row.get("messageId") or "") for row in fresh_records} != set(old_records):
                    raise GenerationError("materialized resume message set changed")

                result_keys = (
                    "declaredSize",
                    "sizeSource",
                    "sourceSizeMismatch",
                    "status",
                    "byteLength",
                    "sha256",
                    "error",
                    "recoveryMethod",
                    "recoveredFromAssetId",
                    "recoveryOriginalError",
                )
                rebound: list[dict[str, Any]] = []
                for fresh_record in fresh_records:
                    message_id = str(fresh_record.get("messageId") or "")
                    old_record = old_records[message_id]
                    old_outcome = validate_record(
                        old_record,
                        require_assets=True,
                        generation_root=stage,
                    )
                    fresh_outcome = validate_record(fresh_record, require_assets=False)
                    if (
                        old_outcome["unknownVisibleFields"]
                        or old_outcome["attachmentErrors"]
                        or fresh_outcome["unknownVisibleFields"]
                        or fresh_outcome["attachmentErrors"]
                        or old_record.get("createdTimestamp") != fresh_record.get("createdTimestamp")
                        or _resume_stable_live_binding(_active_live_binding(old_record))
                        != _resume_stable_live_binding(_active_live_binding(fresh_record))
                    ):
                        raise GenerationError("materialized resume differs from fresh Discord evidence")
                    _old_revision, old_observation = _active_parts(old_record)
                    old_assets = {
                        str(asset.get("assetId") or ""): asset
                        for asset in old_observation.get("assetInventory") or []
                    }
                    observations = [dict(row) for row in fresh_record.get("observations") or []]
                    for observation in observations:
                        assets: list[dict[str, Any]] = []
                        for source_asset in observation.get("assetInventory") or []:
                            asset_id = str(source_asset.get("assetId") or "")
                            old_asset = old_assets.get(asset_id)
                            if old_asset is None:
                                raise GenerationError("materialized resume asset set changed")
                            asset = dict(source_asset)
                            for key in result_keys:
                                if key in old_asset:
                                    asset[key] = old_asset[key]
                                else:
                                    asset.pop(key, None)
                            assets.append(asset)
                        if {str(row.get("assetId") or "") for row in assets} != set(old_assets):
                            raise GenerationError("materialized resume asset set changed")
                        observation["assetInventory"] = assets
                    fresh_record["observations"] = observations
                    fresh_record["attachmentErrors"] = []
                    outcome = validate_record(
                        fresh_record,
                        require_assets=True,
                        generation_root=stage,
                    )
                    if outcome["unknownVisibleFields"] or outcome["attachmentErrors"]:
                        raise GenerationError("materialized resume rebound record failed verification")
                    rebound.append(fresh_record)

                by_day: dict[str, list[dict[str, Any]]] = {}
                for record in rebound:
                    created = _iso_timestamp(
                        record.get("createdTimestamp"),
                        field="createdTimestamp",
                        required=True,
                    )
                    assert created is not None
                    day = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(
                        TZ_TAIPEI
                    ).date().isoformat()
                    by_day.setdefault(day, []).append(record)
                if set(by_day) != set(projection["byDay"]):
                    raise GenerationError("materialized resume day partition changed")

                expected_files = {
                    "receipts/stage-base-current.json",
                    *{f"canonical/{day}.jsonl" for day in by_day},
                    *{f"raw/{day}.md" for day in by_day},
                }
                for old_record in old_records.values():
                    for observation in old_record.get("observations") or []:
                        for asset in observation.get("assetInventory") or []:
                            if asset.get("inScope"):
                                expected_files.add(str(asset.get("localRelativePath") or ""))
                actual_files: set[str] = set()
                for current, dirs, names in os.walk(stage, followlinks=False):
                    current_path = Path(current)
                    if any((current_path / name).is_symlink() for name in dirs):
                        raise GenerationError("materialized resume stage contains a symlink")
                    for name in names:
                        path = current_path / name
                        if not _regular_single_link(path):
                            raise GenerationError("materialized resume stage contains an unsafe file")
                        actual_files.add(path.relative_to(stage).as_posix())
                if actual_files != expected_files:
                    raise GenerationError("materialized resume stage contains unexplained files")

                for day, rows in by_day.items():
                    rows.sort(key=lambda row: int(str(row["messageId"])))
                    self._require_active_lock_lease(lock_token)
                    atomic_jsonl(stage / "canonical" / f"{day}.jsonl", rows)
                    _atomic_bytes(
                        stage / "raw" / f"{day}.md",
                        render_day(rows).encode("utf-8"),
                    )
                rebound_projection = _generation_projection(stage)
                if any(
                    int(rebound_projection[field]) != 0
                    for field in (
                        "duplicateCanonicalIds",
                        "unknownVisibleFields",
                        "attachmentErrors",
                        "sectionCoverageErrors",
                        "markdownErrors",
                    )
                ) or rebound_projection["binaryExpected"] != rebound_projection["binaryVerified"]:
                    raise GenerationError("materialized resume rebound stage failed verification")
                _verified_generation_assets(stage)
                stable_bindings = [
                    _resume_stable_live_binding(_active_live_binding(row))
                    for row in sorted(rebound, key=lambda item: int(str(item["messageId"])))
                ]
                return {
                    "records": len(rebound),
                    "assetFileCount": int(rebound_projection["binaryVerified"]),
                    "resumeStableBindingSha256": json_sha256(stable_bindings),
                    "freshEvidenceSha256": str(registration.get("evidenceSha256") or ""),
                }
        except BaseException:
            _consume_live_evidence_token(live_evidence_token)
            raise
        finally:
            _end_active_run_entry_lease(run_lease)

    def reserve_full_stage_assets(
        self,
        stage: Path,
        *,
        run_context: FullRebuildRunContext,
        channel_id: str,
        relative_path: str,
        lock_token: ArchiveLockToken | None = None,
    ) -> dict[str, Any]:
        """Bind verified staged attachment bytes to the one shared full-run budget."""
        identity = _validated_entry_identity({
            "channelId": str(channel_id),
            "relativePath": relative_path,
            "normalizedRelativePath": unicodedata.normalize("NFKC", relative_path).casefold(),
        })
        registration = _require_run_context(
            run_context,
            kind="full_rebuild",
            identity=identity,
            entry_root=self.entry_root,
        )
        if lock_token is None:
            lock_token = registration["lockToken"]
        registration = _require_run_context(
            run_context,
            kind="full_rebuild",
            identity=identity,
            entry_root=self.entry_root,
            lock_token=lock_token,
        )
        with _borrow_active_run_entry(self.entry_root, lock_token), self._borrow_lock(lock_token):
            registration = _require_run_context(
                run_context,
                kind="full_rebuild",
                identity=identity,
                entry_root=self.entry_root,
                lock_token=lock_token,
            )
            _require_active_run_entry_lock(self.entry_root, lock_token)
            stage = _lexical_absolute(stage)
            entry_root, generation_id = _generation_binding(stage)
            if (
                entry_root != self.entry_root
                or stage.parent != self.staging
                or stage.is_symlink()
                or not stage.is_dir()
            ):
                raise AssetDownloadError("asset reservation requires an owned staging generation")
            with registration["mutex"]:
                if identity["channelId"] in registration["assetReservations"]:
                    raise AssetDownloadError("full-run entry already has an asset reservation")
                assets, asset_evidence = _verified_generation_assets(stage)
                asset_file_count = len(assets)
                asset_declared_bytes = sum(int(asset["declaredSize"]) for asset in assets)
                reservation: dict[str, Any] = {
                    "schemaVersion": ASSET_RESERVATION_SCHEMA,
                    "reservationId": secrets.token_hex(32),
                    "runContextId": registration["runContextId"],
                    "archiveRootSha256": registration["archiveRootSha256"],
                    "inventoryDigest": registration["inventoryDigest"],
                    "entryIdentity": identity,
                    "entryRootSha256": json_sha256(str(entry_root)),
                    "generationId": generation_id,
                    "assetFileCount": asset_file_count,
                    "assetDeclaredBytes": asset_declared_bytes,
                    "assets": asset_evidence,
                    "assetsSha256": json_sha256(asset_evidence),
                }
                reservation["receiptSha256"] = json_sha256(reservation)
                receipt_path = contained_path(
                    stage,
                    "receipts/full-run-asset-reservation.json",
                )
                if os.path.lexists(receipt_path):
                    raise AssetDownloadError("full-run asset reservation receipt already exists")
                self._require_active_lock_lease(lock_token)
                atomic_json(receipt_path, reservation)
                try:
                    capacity = registration["budget"].reserve_entry(
                        assets,
                        stage,
                        assume_unknown_max=False,
                        assets_materialized=True,
                    )
                except BaseException:
                    receipt_path.unlink(missing_ok=True)
                    raise
                if (
                    capacity["files"] != asset_file_count
                    or capacity["declaredBytes"] != asset_declared_bytes
                ):
                    registration["budget"].release_entry(capacity)
                    receipt_path.unlink(missing_ok=True)
                    raise AssetDownloadError("asset reservation totals changed during commit")
                registration["assetReservations"][identity["channelId"]] = json.loads(
                    json.dumps(reservation, ensure_ascii=False)
                )
                return reservation

    def install_full_pass_evidence(
        self,
        stage: Path,
        *,
        live_evidence_token: LiveEvidenceToken | None,
        lock_token: ArchiveLockToken | None = None,
    ) -> dict[str, Any]:
        """Install collector evidence; only the live token can authorize PASS."""
        lease: dict[str, Any] | None = None
        run_lease: dict[str, Any] | None = None
        try:
            initial_registration = _require_live_evidence_token(
                live_evidence_token,
                root=stage,
                allowed_states={"fresh"},
            )
            run_registration = _require_run_context(
                initial_registration["runContext"](),
                kind="full_rebuild",
                entry_root=initial_registration["entryRoot"],
            )
            if lock_token is None:
                lock_token = run_registration["lockToken"]
            _require_run_context(
                initial_registration["runContext"](),
                kind="full_rebuild",
                entry_root=initial_registration["entryRoot"],
                lock_token=lock_token,
            )
            run_lease = _begin_active_run_entry_lease(self.entry_root, lock_token)
            lease = self._begin_lock_lease(lock_token)
            stage = _lexical_absolute(stage)
            registration = _require_live_evidence_token(
                live_evidence_token,
                root=stage,
                allowed_states={"fresh"},
            )
            evidence = json.loads(json.dumps(registration["evidence"], ensure_ascii=False))
            if self.staging not in stage.parents or stage.parent != self.staging:
                raise GenerationError("full PASS evidence may only be installed on an owned stage")
            reject_symlink_path(stage)
            if not stage.is_dir():
                raise GenerationError("full PASS evidence stage is missing")
            evidence_path = contained_path(stage, "receipts/live-inventory-evidence.json")
            self._require_active_lock_lease(lock_token)
            atomic_json(evidence_path, evidence)
            runtime_receipt = _build_runtime_pass_receipt(stage, evidence)
            if runtime_receipt.get("gateStatus") != "PASS":
                raise GenerationError("concrete live evidence did not satisfy the full PASS gate")
            audit_receipt = _persisted_audit_receipt(runtime_receipt)
            atomic_json(stage / "receipts" / "rich-archive-latest.json", audit_receipt)
            manifest = generation_inventory(stage)
            atomic_json(stage / "generation-manifest.json", manifest)
            verified = verify_full_generation(
                stage,
                live_evidence_token=live_evidence_token,
            )
            if not verified["ok"]:
                raise GenerationError("full PASS evidence stage failed local verification")
            registration["state"] = "prepared"
            registration["preparedGenerationSha256"] = manifest["generationSha256"]
            registration["preparedAssetReservationId"] = verified["runtimeReceipt"][
                "assetReservationId"
            ]
            return {
                "receipt": runtime_receipt,
                "auditReceipt": audit_receipt,
                "manifest": manifest,
                "verified": verified,
            }
        except BaseException:
            _consume_live_evidence_token(live_evidence_token)
            raise
        finally:
            if lease is not None:
                self._end_lock_lease(lease)
            _end_active_run_entry_lease(run_lease)

    def seal_full_stage_for_root_run(
        self,
        stage: Path,
        generation_id: str,
        generation_sha256: str,
        *,
        live_evidence_token: LiveEvidenceToken | None,
        lock_token: ArchiveLockToken | None = None,
        run_context: FullRebuildRunContext,
    ) -> Path:
        """Seal a verified full generation without changing per-entry CURRENT.

        A full rebuild first seals every entry, then atomically selects one
        archive-root manifest.  Compatibility CURRENT pointers are published
        only after that root selection is read back.
        """
        initial = _require_live_evidence_token(
            live_evidence_token,
            root=stage,
            allowed_states={"prepared"},
        )
        if initial["runContext"]() is not run_context:
            raise RichArchiveError("full seal run context does not match live evidence")
        run_registration = _require_run_context(
            run_context,
            kind="full_rebuild",
            entry_root=self.entry_root,
        )
        if lock_token is None:
            lock_token = run_registration["lockToken"]
        _require_run_context(
            run_context,
            kind="full_rebuild",
            entry_root=self.entry_root,
            lock_token=lock_token,
        )
        run_lease = _begin_active_run_entry_lease(self.entry_root, lock_token)
        lease: dict[str, Any] | None = None
        try:
            lease = self._begin_lock_lease(lock_token)
            stage = _lexical_absolute(stage)
            generation_id = _validated_generation_id(generation_id)
            if not re.fullmatch(r"[0-9a-f]{64}", generation_sha256):
                raise GenerationError("invalid generation checksum")
            final = contained_path(self.generations, generation_id)
            expected_stage = contained_path(self.staging, generation_id)
            if stage != expected_stage or stage.is_symlink() or not stage.is_dir():
                raise GenerationError("seal stage path is invalid")
            token_registration = _require_live_evidence_token(
                live_evidence_token,
                root=stage,
                allowed_states={"prepared"},
            )
            if token_registration.get("preparedGenerationSha256") != generation_sha256:
                raise GenerationError(
                    "live evidence token is not prepared for this generation hash"
                )
            verified = _verify_full_generation_runtime(
                stage,
                live_evidence_token=live_evidence_token,
                allowed_states={"prepared"},
            )
            if not verified.get("ok") or verified.get("generationSha256") != generation_sha256:
                raise GenerationError("sealed stage failed runtime verification")
            if final.exists() or final.is_symlink():
                raise GenerationError("generation destination already exists")
            identity = _validated_entry_identity(
                token_registration["evidence"].get("entryIdentity")
            )
            reservation_id = str(
                verified["runtimeReceipt"].get("assetReservationId") or ""
            )
            with run_registration["mutex"]:
                if identity["channelId"] in run_registration["processed"]:
                    raise GenerationError("full rebuild entry was already sealed in this run")
                if reservation_id in run_registration["usedAssetReservations"]:
                    raise GenerationError("full-run asset reservation was already consumed")
            self._require_active_lock_lease(lock_token)
            _require_active_run_entry_lock(self.entry_root, lock_token)
            self._require_stage_base_current_unchanged(stage)
            os.replace(stage, final)
            _fsync_dir(self.generations)
            with run_registration["mutex"]:
                run_registration["usedAssetReservations"].add(reservation_id)
                run_registration["processed"][identity["channelId"]] = generation_sha256
                run_registration["sealedRoots"][identity["channelId"]] = str(final)
            return final
        finally:
            try:
                if lease is not None:
                    self._end_lock_lease(lease)
            finally:
                try:
                    _end_active_run_entry_lease(run_lease)
                finally:
                    _consume_live_evidence_token(live_evidence_token)

    def register_existing_sealed_generation_for_root_run(
        self,
        final: Path,
        generation_id: str,
        generation_sha256: str,
        *,
        live_evidence_token: LiveEvidenceToken | None,
        lock_token: ArchiveLockToken | None = None,
        run_context: FullRebuildRunContext,
    ) -> dict[str, Any]:
        """Re-register an immutable generation after fresh Discord readback.

        Persisted audit receipts never authorize resume on their own.  The
        caller must freshly enumerate the same entry under a live evidence
        token.  Only an exact active-binding/cutoff match may reuse the already
        verified local bytes and charge them to the new run-wide budget.
        """
        token_registration = _require_live_evidence_token(
            live_evidence_token,
            root=final,
            allowed_states={"fresh"},
        )
        if token_registration["runContext"]() is not run_context:
            raise RichArchiveError("resume run context does not match live evidence")
        identity = _validated_entry_identity(
            token_registration["evidence"].get("entryIdentity")
        )
        run_registration = _require_run_context(
            run_context,
            kind="full_rebuild",
            identity=identity,
            entry_root=self.entry_root,
        )
        if lock_token is None:
            lock_token = run_registration["lockToken"]
        _require_run_context(
            run_context,
            kind="full_rebuild",
            identity=identity,
            entry_root=self.entry_root,
            lock_token=lock_token,
        )
        run_lease = _begin_active_run_entry_lease(self.entry_root, lock_token)
        lease: dict[str, Any] | None = None
        capacity: dict[str, int] | None = None
        try:
            lease = self._begin_lock_lease(lock_token)
            generation_id = _validated_generation_id(generation_id)
            expected_final = contained_path(self.generations, generation_id)
            final = _lexical_absolute(final)
            if final != expected_final or final.is_symlink() or not final.is_dir():
                raise GenerationError("resume generation path is invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", generation_sha256):
                raise GenerationError("invalid generation checksum")
            local = verify_generation(final)
            if (
                not local.get("ok")
                or local.get("generationSha256") != generation_sha256
                or local.get("gateStatus") != "AUDIT_ONLY"
                or not local.get("fullGatePresent")
                or local.get("fullGateErrors")
            ):
                raise GenerationError("resume generation audit verification failed")
            evidence_path = contained_path(
                final,
                "receipts/live-inventory-evidence.json",
            )
            if not _regular_single_link(evidence_path):
                raise GenerationError("resume generation live evidence is missing")
            persisted_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            fresh_evidence = token_registration["evidence"]
            persisted_inventory = persisted_evidence.get("inventory")
            fresh_inventory = fresh_evidence.get("inventory")
            persisted_transaction = persisted_evidence.get("transactionBinding")
            fresh_records = token_registration.get("normalizedRecords")
            if not isinstance(fresh_records, list):
                raise GenerationError("resume fresh normalized records are missing")
            for record in fresh_records:
                if not isinstance(record, Mapping):
                    raise GenerationError("resume fresh normalized record is invalid")
                validate_record(record, require_assets=False)
                if record.get("unknownVisibleFields") or record.get("attachmentErrors"):
                    raise GenerationError("resume fresh record has unresolved visible fields")
            persisted_messages = persisted_evidence.get("messages")
            fresh_messages = fresh_evidence.get("messages")
            if not isinstance(persisted_messages, list) or not isinstance(fresh_messages, list):
                raise GenerationError("resume live message evidence is invalid")
            persisted_stable = [
                _resume_stable_live_binding(row) for row in persisted_messages
            ]
            fresh_stable = [
                _resume_stable_live_binding(row) for row in fresh_messages
            ]
            if (
                not isinstance(persisted_inventory, Mapping)
                or not isinstance(fresh_inventory, Mapping)
                or not isinstance(persisted_transaction, Mapping)
                or persisted_evidence.get("entryIdentity") != fresh_evidence.get("entryIdentity")
                or persisted_evidence.get("verifiedCutoff") != fresh_evidence.get("verifiedCutoff")
                or persisted_stable != fresh_stable
                or persisted_inventory.get("digest") != run_registration["inventoryDigest"]
                or fresh_inventory.get("digest") != run_registration["inventoryDigest"]
                or persisted_transaction.get("generationId") != generation_id
            ):
                raise GenerationError("resume generation differs from fresh Discord evidence")
            reservation = _validated_persisted_asset_reservation(final, identity)
            assets, _asset_evidence = _verified_generation_assets(final)
            capacity = run_registration["budget"].reserve_entry(
                assets,
                final,
                assume_unknown_max=False,
                assets_materialized=True,
            )
            if (
                capacity["files"] != reservation.get("assetFileCount")
                or capacity["declaredBytes"] != reservation.get("assetDeclaredBytes")
            ):
                raise AssetDownloadError("resume generation asset totals changed")
            reservation_id = str(reservation.get("reservationId") or "")
            with run_registration["mutex"]:
                channel_id = identity["channelId"]
                if (
                    channel_id in run_registration["processed"]
                    or channel_id in run_registration["assetReservations"]
                    or reservation_id in run_registration["usedAssetReservations"]
                ):
                    raise GenerationError("resume generation was already registered")
                run_registration["assetReservations"][channel_id] = json.loads(
                    json.dumps(reservation, ensure_ascii=False)
                )
                run_registration["usedAssetReservations"].add(reservation_id)
                run_registration["processed"][channel_id] = generation_sha256
                run_registration["sealedRoots"][channel_id] = str(final)
            capacity = None
            return {
                "generationSha256": generation_sha256,
                "records": int(local.get("records") or 0),
                "assetFileCount": int(reservation["assetFileCount"]),
                "assetDeclaredBytes": int(reservation["assetDeclaredBytes"]),
                "resumeStableBindingSha256": json_sha256(fresh_stable),
                "freshEvidenceSha256": str(fresh_evidence.get("evidenceSha256") or ""),
            }
        finally:
            try:
                if capacity is not None:
                    run_registration["budget"].release_entry(capacity)
            finally:
                try:
                    if lease is not None:
                        self._end_lock_lease(lease)
                finally:
                    try:
                        _end_active_run_entry_lease(run_lease)
                    finally:
                        _consume_live_evidence_token(live_evidence_token)

    def publish_existing_generation_pointer(
        self,
        generation_id: str,
        generation_sha256: str,
        *,
        lock_token: ArchiveLockToken | None = None,
        run_context: FullRebuildRunContext,
    ) -> dict[str, Any]:
        """Publish and exact-read back a compatibility CURRENT pointer."""
        registration = _require_run_context(
            run_context,
            kind="full_rebuild",
            entry_root=self.entry_root,
        )
        if lock_token is None:
            lock_token = registration["lockToken"]
        _require_run_context(
            run_context,
            kind="full_rebuild",
            entry_root=self.entry_root,
            lock_token=lock_token,
        )
        generation_id = _validated_generation_id(generation_id)
        final = contained_path(self.generations, generation_id)
        with _borrow_active_run_entry(self.entry_root, lock_token), self._borrow_lock(lock_token):
            verified = verify_generation(final)
            if verified.get("generationSha256") != generation_sha256:
                raise GenerationError("compatibility generation checksum mismatch")
            self._require_active_lock_lease(lock_token)
            pointer = self._pointer_body(generation_id, generation_sha256)
            pointer["pointerSha256"] = json_sha256(pointer)
            atomic_json(self.pointer_path, pointer)
            current = self.resolve_current()
            if current is None or current != final:
                raise GenerationError("compatibility CURRENT readback failed")
            return json.loads(self.pointer_path.read_text(encoding="utf-8"))

    def publish_stage(
        self,
        stage: Path,
        generation_id: str,
        generation_sha256: str,
        *,
        require_full_gate: bool = False,
        live_evidence_token: LiveEvidenceToken | None = None,
        lock_token: ArchiveLockToken | None = None,
        run_context: ArchiveRunContext | None = None,
    ) -> None:
        full_registration: dict[str, Any] | None = None
        run_registration: dict[str, Any] | None = None
        processed_channel: str | None = None
        asset_reservation_id: str | None = None
        lease: dict[str, Any] | None = None
        run_lease: dict[str, Any] | None = None
        try:
            if require_full_gate:
                initial_full_registration = _require_live_evidence_token(
                    live_evidence_token,
                    root=stage,
                    allowed_states={"prepared"},
                )
                evidence_run_context = initial_full_registration["runContext"]()
                if run_context is not None and run_context is not evidence_run_context:
                    raise RichArchiveError(
                        "full publish run context does not match live evidence"
                    )
                run_context = evidence_run_context
                _require_run_context(
                    run_context,
                    kind="full_rebuild",
                    entry_root=initial_full_registration["entryRoot"],
                )
            _, lock_token = self._managed_run_registration(run_context, lock_token)
            run_lease = _begin_active_run_entry_lease(self.entry_root, lock_token)
            lease = self._begin_lock_lease(lock_token)
            generation_id = _validated_generation_id(generation_id)
            if not re.fullmatch(r"[0-9a-f]{64}", generation_sha256):
                raise GenerationError("invalid generation checksum")
            final = contained_path(self.generations, generation_id)
            expected_stage = contained_path(self.staging, generation_id)
            if _lexical_absolute(stage) != expected_stage or stage.is_symlink() or not stage.is_dir():
                raise GenerationError("publish stage path is invalid")
            verified = verify_generation(stage)
            if require_full_gate:
                full_registration = _require_live_evidence_token(
                    live_evidence_token,
                    root=stage,
                    allowed_states={"prepared"},
                )
                if full_registration.get("preparedGenerationSha256") != generation_sha256:
                    raise GenerationError("live evidence token is not prepared for this generation hash")
                full_registration["state"] = "publishing"
                verified = _verify_full_generation_runtime(
                    stage,
                    live_evidence_token=live_evidence_token,
                    allowed_states={"publishing"},
                )
                run_context = full_registration["runContext"]()
                identity = _validated_entry_identity(
                    full_registration["evidence"].get("entryIdentity")
                )
                run_registration = _require_run_context(
                    run_context,
                    kind="full_rebuild",
                    identity=identity,
                    entry_root=full_registration["entryRoot"],
                    lock_token=lock_token,
                )
                processed_channel = identity["channelId"]
                asset_reservation_id = str(
                    verified["runtimeReceipt"].get("assetReservationId") or ""
                )
                if full_registration.get("preparedAssetReservationId") != asset_reservation_id:
                    raise GenerationError("live evidence token asset reservation binding changed")
                with run_registration["mutex"]:
                    if processed_channel in run_registration["processed"]:
                        raise GenerationError("full rebuild entry was already published in this run")
            if not verified["ok"] or verified["generationSha256"] != generation_sha256:
                raise GenerationError("publish stage failed generation verification")
            if final.exists() or final.is_symlink():
                raise GenerationError("generation destination already exists")
            if require_full_gate:
                refreshed_registration = _require_live_evidence_token(
                    live_evidence_token,
                    root=stage,
                    allowed_states={"publishing"},
                )
                if refreshed_registration is not full_registration:
                    raise GenerationError("runtime live evidence reservation changed before commit")
            self._require_active_lock_lease(lock_token)
            _require_active_run_entry_lock(self.entry_root, lock_token)
            self._require_stage_base_current_unchanged(stage)
            journal = {
                "schemaVersion": JOURNAL_SCHEMA,
                "phase": "prepared",
                "generationId": generation_id,
                "generationSha256": generation_sha256,
                "previousPointerSha256": file_sha256(self.pointer_path) if self.pointer_path.exists() else None,
            }
            _write_journal(self.journal_path, journal)
            self._require_active_lock_lease(lock_token)
            os.replace(stage, final)
            _fsync_dir(self.generations)
            journal["phase"] = "generation_ready"
            _write_journal(self.journal_path, journal)
            self._require_active_lock_lease(lock_token)
            pointer = self._pointer_body(generation_id, generation_sha256)
            pointer["pointerSha256"] = json_sha256(pointer)
            atomic_json(self.pointer_path, pointer)
            journal["phase"] = "committed"
            journal["committedPointerSha256"] = file_sha256(self.pointer_path)
            _write_journal(self.journal_path, journal)
            if require_full_gate:
                assert (
                    run_registration is not None
                    and processed_channel is not None
                    and asset_reservation_id is not None
                )
                with run_registration["mutex"]:
                    if asset_reservation_id in run_registration["usedAssetReservations"]:
                        raise GenerationError("full-run asset reservation was already consumed")
                    run_registration["usedAssetReservations"].add(asset_reservation_id)
                    run_registration["processed"][processed_channel] = generation_sha256
        finally:
            try:
                if lease is not None:
                    self._end_lock_lease(lease)
            finally:
                try:
                    _end_active_run_entry_lease(run_lease)
                finally:
                    if require_full_gate:
                        _consume_live_evidence_token(live_evidence_token)

    def recover_journal(
        self,
        *,
        lock_token: ArchiveLockToken | None = None,
        run_context: ArchiveRunContext | None = None,
    ) -> dict[str, Any]:
        _, lock_token = self._managed_run_registration(run_context, lock_token)
        with _borrow_active_run_entry(self.entry_root, lock_token), self._borrow_lock(lock_token):
            _require_active_run_entry_lock(self.entry_root, lock_token)
            return self._recover_journal_under_lease(lock_token=lock_token)

    def _recover_journal_under_lease(
        self,
        *,
        lock_token: ArchiveLockToken | None,
    ) -> dict[str, Any]:
        self._require_active_lock_lease(lock_token)
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
            self._require_active_lock_lease(lock_token)
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
    "ArchiveLockToken", "ArchiveRunContext", "AssetDownloadError", "AssetDownloader",
    "AssetLimits", "AssetProbeBudget", "ASSET_RESERVATION_SCHEMA",
    "CANONICAL_ARCHIVE_LOCK_NAME", "DEFAULT_CDN_HOSTS",
    "DISCORD_INVENTORY_RESPONSE_SCHEMA", "DISCORD_PAGE_RESPONSE_SCHEMA", "ENTRY_RECEIPT_SCHEMA",
    "FULL_RUN_RECEIPT_SCHEMA", "FullRebuildRunContext", "GENERATION_MANIFEST_SCHEMA",
    "GenerationError", "IncrementalRunContext", "LIVE_EVIDENCE_SCHEMA", "RECORD_SCHEMA",
    "LiveEvidenceToken", "RICH_CORE_CONTRACT", "RichArchiveError", "RichArchiveStore", "SOURCE_CENSUS_SCHEMA",
    "STAGE_BASE_SCHEMA",
    "SourceBoundsError", "SourceCensusError", "apply_asset_results", "atomic_json",
    "atomic_jsonl", "begin_full_rebuild_run", "begin_incremental_run",
    "canonical_archive_lock_path", "canonical_day", "collect_live_evidence",
    "contained_path", "file_sha256", "finalize_full_rebuild_run",
    "generation_inventory",
    "inventory_assets", "json_sha256", "load_jsonl",
    "merge_day_records", "merge_message_records", "normalize_message",
    "parse_markdown_markers", "preflight_asset_capacity", "render_day",
    "render_message", "required_render_sections", "resolve_asset_sizes",
    "renderer_pointer_accounting", "sanitize_lossless_source", "source_field_census",
    "validate_record", "verify_full_generation", "verify_generation",
    "verify_sealed_full_rebuild_run",
]
