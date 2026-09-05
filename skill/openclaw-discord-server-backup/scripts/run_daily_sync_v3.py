#!/usr/bin/env python3
"""Bounded deterministic daily Discord rich-sync runner (v2 scaffold).

The transport, inventory, selection, and queue contracts live here.  The rich
archive adapter intentionally fails closed until the independently reviewed v2
core exports its final slot/session API; no compatibility shim may guess that
contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


INVENTORY_BINDING_SCHEMA = "openclaw-discord-daily-inventory-binding.v2"
ENTRY_BINDING_SCHEMA = "openclaw-discord-daily-entry-binding.v2"
CURRENT_SNAPSHOT_SCHEMA = "openclaw-discord-rich-current-snapshot.v2"
MERGE_COMMIT_SCHEMA = "openclaw-discord-rich-incremental-commit.v2"
ACTIVE_QUEUE_STATUSES = frozenset({"queued", "catching_up", "retry"})
INELIGIBLE_ENTRY_STATUSES = frozenset(
    {"partial", "queued", "catching_up", "retry", "error", "excluded"}
)
QUEUE_REASONS = frozenset({
    "rich_full_rebuild_required",
    "rich_archive_repair_required",
    "rich_incremental_partial",
    "rich_incremental_read_error",
    "rich_incremental_merge_error",
    "rich_incremental_readback_error",
    "rich_mutable_refresh_pending",
})
ERROR_REASONS = frozenset({
    "invalid_limits",
    "invalid_today",
    "invalid_timezone",
    "unsafe_input_path",
    "missing_input_file",
    "missing_input_directory",
    "unreadable_state",
    "unreadable_queue",
    "unreadable_inventory",
    "unreadable_mapping_ledger",
    "invalid_state",
    "invalid_queue",
    "inventory_stale",
    "inventory_incomplete",
    "inventory_identity_mismatch",
    "inventory_digest_mismatch",
    "backup_lock_busy",
    "discord_auth_unavailable",
    "invalid_discord_identity",
    "discord_fetch_failed",
    "discord_response_invalid",
    "discord_response_too_large",
    "discord_rate_limit_exhausted",
    "discord_duplicate_conflict",
    "rich_core_contract_pending",
    "rich_core_contract_unsupported",
    "rich_baseline_missing",
    "rich_archive_merge_failed",
    "rich_archive_readback_failed",
    "queue_persist_failed",
    "state_persist_failed",
    "journal_recovery_failed",
    "unexpected_error",
})
MAX_429_RETRIES = 8
MAX_429_WAIT_SECONDS = 120.0
MAX_DISCORD_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DISCORD_ERROR_BYTES = 64 * 1024
SNOWFLAKE_RE = re.compile(r"^[0-9]{1,32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class DailySyncError(RuntimeError):
    """A bounded operator-safe error with a fixed public category."""

    def __init__(self, category: str):
        if category not in ERROR_REASONS:
            category = "unexpected_error"
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class MutableRefreshPlan:
    target_message_id: str | None
    next_scan_cursor: str | None
    cycle_completed: bool


class RichSlotSession(Protocol):
    """Future v2 rich-core port; implemented only after core review passes."""

    def inspect_current(self, entry_root: Path, entry_binding: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def normalize_source_bindings(
        self, messages: Sequence[Mapping[str, Any]], *, channel_id: str, observed_at: str
    ) -> Mapping[str, str]: ...

    def merge_incremental(self, **operation: Any) -> Mapping[str, Any]: ...


class RichSlotFactory(Protocol):
    def open_slot(self, *, lock_path: Path) -> AbstractContextManager[RichSlotSession]: ...


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def reject_control_characters(value: str, label: str) -> None:
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise DailySyncError("unsafe_input_path" if "path" in label else "inventory_identity_mismatch")


def reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise DailySyncError("unsafe_input_path")


def safe_path(
    value: str | Path,
    *,
    require_file: bool = False,
    require_directory: bool = False,
) -> Path:
    text = str(value)
    reject_control_characters(text, "path")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise DailySyncError("unsafe_input_path")
    path = Path(os.path.abspath(candidate))
    reject_symlink_components(path)
    if path == Path(path.anchor):
        raise DailySyncError("unsafe_input_path")
    if require_file and (not path.is_file() or path.is_symlink()):
        raise DailySyncError("missing_input_file")
    if require_directory and (not path.is_dir() or path.is_symlink()):
        raise DailySyncError("missing_input_directory")
    return path


def safe_entry_root(root: Path, relative_path: str) -> Path:
    reject_control_characters(relative_path, "path")
    pure = PurePosixPath(relative_path)
    if (
        not relative_path
        or pure.is_absolute()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise DailySyncError("unsafe_input_path")
    candidate = Path(os.path.abspath(root.joinpath(*pure.parts)))
    reject_symlink_components(candidate)
    if candidate != root and root not in candidate.parents:
        raise DailySyncError("unsafe_input_path")
    return candidate


def load_json_object(path: Path, category: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DailySyncError(category) from exc
    if not isinstance(payload, dict):
        raise DailySyncError(category)
    return payload


def atomic_json(path: Path, payload: Mapping[str, Any], *, failure_category: str) -> None:
    reject_symlink_components(path)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise DailySyncError(failure_category)
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
        raise DailySyncError(failure_category)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = -1
    hipath = ""
    try:
        descriptor, hipath = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=path.parent
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(hipath, path)
        hipath = ""
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise DailySyncError(failure_category) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if hipath:
            try:
                os.unlink(hipath)
            except FileNotFoundError:
                pass


def persist_queue_then_state(
    queue_path: Path,
    queue: Mapping[str, Any],
    state_path: Path,
    state: Mapping[str, Any],
    *,
    after_queue: Callable[[], None] | None = None,
) -> None:
    """Persist retry ownership before any cursor-bearing state replacement.

    The optional hook is only a deterministic fault-injection seam.  If it
    raises, queue ownership remains durable while the old state/cursor remains
    selected, so the next invocation performs a safe idempotent replay.
    """
    atomic_json(queue_path, queue, failure_category="queue_persist_failed")
    if after_queue is not None:
        after_queue()
    atomic_json(state_path, state, failure_category="state_persist_failed")


def _parse_timestamp(value: Any, category: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DailySyncError(category) from exc
    if parsed.tzinfo is None:
        raise DailySyncError(category)
    return parsed


def _normalized_relative_path(value: str) -> str:
    reject_control_characters(value, "path")
    return unicodedata.normalize("NFKC", value).casefold()


def _canonical_inventory_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    channels = payload.get("channels")
    threads = payload.get("threads")
    if not isinstance(channels, list) or not isinstance(threads, list):
        raise DailySyncError("inventory_incomplete")
    for kind, values in (("channel", channels), ("thread", threads)):
        for value in values:
            if not isinstance(value, Mapping):
                raise DailySyncError("inventory_incomplete")
            channel_id = str(value.get("id") or "")
            if not SNOWFLAKE_RE.fullmatch(channel_id):
                raise DailySyncError("inventory_identity_mismatch")
            row = {"channelId": channel_id, "type": kind}
            if kind == "thread":
                parent_id = str(value.get("parent_id") or "")
                if not SNOWFLAKE_RE.fullmatch(parent_id):
                    raise DailySyncError("inventory_identity_mismatch")
                row["parentId"] = parent_id
            rows.append(row)
    ids = [row["channelId"] for row in rows]
    if len(ids) != len(set(ids)):
        raise DailySyncError("inventory_identity_mismatch")
    return sorted(rows, key=lambda row: int(row["channelId"]))


def validate_inventory_binding(
    payload: Mapping[str, Any],
    mapping: Mapping[str, Any],
    *,
    guild_id: str,
    today: str,
    timezone_name: str,
) -> dict[str, Any]:
    if not SNOWFLAKE_RE.fullmatch(guild_id):
        raise DailySyncError("inventory_identity_mismatch")
    try:
        zone = ZoneInfo(timezone_name)
        current_day = date.fromisoformat(today)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DailySyncError("invalid_timezone" if isinstance(exc, ZoneInfoNotFoundError) else "invalid_today") from exc
    checked = _parse_timestamp(payload.get("checkedAt"), "inventory_stale")
    generated = _parse_timestamp(mapping.get("generatedAt"), "inventory_stale")
    if checked.astimezone(zone).date() != current_day or generated.astimezone(zone).date() != current_day:
        raise DailySyncError("inventory_stale")
    coverage = payload.get("coverage")
    if (
        payload.get("ok") is not True
        or str(payload.get("guildId") or "") != guild_id
        or payload.get("remainingMissing") != 0
        or not isinstance(payload.get("warnings"), list)
        or payload.get("warnings")
        or not isinstance(coverage, Mapping)
        or coverage.get("archivedEnumerationStatus") != "complete"
        or mapping.get("schema") != "openclaw-discord-inventory-mapping-v1"
        or mapping.get("applyAllowed") is not True
        or not isinstance(mapping.get("blockers"), list)
        or mapping.get("blockers")
    ):
        raise DailySyncError("inventory_incomplete")
    live_rows = _canonical_inventory_rows(payload)
    mapping_rows = mapping.get("entries")
    if not isinstance(mapping_rows, list):
        raise DailySyncError("inventory_incomplete")
    normalized_mapping: list[dict[str, Any]] = []
    for row in mapping_rows:
        if not isinstance(row, Mapping):
            raise DailySyncError("inventory_incomplete")
        channel_id = str(row.get("channelId") or "")
        relative_path = str(row.get("relativePath") or "")
        if (
            not SNOWFLAKE_RE.fullmatch(channel_id)
            or row.get("type") not in {"channel", "thread"}
            or row.get("safePath") is not True
            or row.get("decision") == "blocked"
        ):
            raise DailySyncError("inventory_identity_mismatch")
        safe_entry_root(Path("/inventory-binding-root"), relative_path)
        normalized_mapping.append({
            "channelId": channel_id,
            "type": row["type"],
            "relativePath": relative_path,
            "normalizedRelativePath": _normalized_relative_path(relative_path),
        })
    normalized_mapping.sort(key=lambda row: int(row["channelId"]))
    if [row["channelId"] for row in normalized_mapping] != [row["channelId"] for row in live_rows]:
        raise DailySyncError("inventory_identity_mismatch")
    if any(
        mapped["type"] != live["type"]
        for mapped, live in zip(normalized_mapping, live_rows)
    ):
        raise DailySyncError("inventory_identity_mismatch")
    body = {
        "schemaVersion": INVENTORY_BINDING_SCHEMA,
        "guildId": guild_id,
        "observedAt": checked.astimezone(timezone.utc).isoformat(),
        "mappingGeneratedAt": generated.astimezone(timezone.utc).isoformat(),
        "entries": normalized_mapping,
        "liveInventory": live_rows,
    }
    body["inventoryDigest"] = json_sha256(body)
    return body


def bind_entry_inventory(
    inventory: Mapping[str, Any], *, channel_id: str, relative_path: str, entry_type: str
) -> dict[str, Any]:
    rows = inventory.get("entries")
    if not isinstance(rows, list):
        raise DailySyncError("inventory_incomplete")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("channelId") == channel_id]
    if len(matches) != 1:
        raise DailySyncError("inventory_identity_mismatch")
    row = matches[0]
    normalized = _normalized_relative_path(relative_path)
    if row.get("type") != entry_type or row.get("normalizedRelativePath") != normalized:
        raise DailySyncError("inventory_identity_mismatch")
    body = {
        "schemaVersion": ENTRY_BINDING_SCHEMA,
        "guildId": inventory.get("guildId"),
        "channelId": channel_id,
        "type": entry_type,
        "relativePath": relative_path,
        "normalizedRelativePath": normalized,
        "inventoryDigest": inventory.get("inventoryDigest"),
        "inventoryObservedAt": inventory.get("observedAt"),
    }
    body["entryBindingSha256"] = json_sha256(body)
    return body


def snowflake_int(value: Any) -> int:
    text = str(value or "")
    return int(text) if SNOWFLAKE_RE.fullmatch(text) else 0


def newest_cursor(*values: Any) -> str | None:
    valid = [str(value) for value in values if snowflake_int(value) > 0]
    return max(valid, key=snowflake_int) if valid else None


def entry_is_excluded(entry: Mapping[str, Any]) -> bool:
    return bool(
        entry.get("backupExcluded")
        or entry.get("invalidChannel")
        or entry.get("syncStatus") == "excluded"
    )


def active_queue_keys(queue: Mapping[str, Any]) -> set[str]:
    items = queue.get("items")
    if not isinstance(items, list):
        raise DailySyncError("invalid_queue")
    keys: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise DailySyncError("invalid_queue")
        key = item.get("entryKey")
        if isinstance(key, str) and item.get("status", "queued") in ACTIVE_QUEUE_STATUSES:
            keys.add(key)
    return keys


def select_candidates(
    state: Mapping[str, Any], queue: Mapping[str, Any], *, today: str, max_entries: int
) -> list[tuple[str, dict[str, Any]]]:
    try:
        date.fromisoformat(today)
    except ValueError as exc:
        raise DailySyncError("invalid_today") from exc
    entries = state.get("entries")
    if not isinstance(entries, Mapping):
        raise DailySyncError("invalid_state")
    active = active_queue_keys(queue)
    selected: list[tuple[str, dict[str, Any]]] = []
    for key, entry_value in entries.items():
        if not isinstance(key, str) or not isinstance(entry_value, Mapping):
            raise DailySyncError("invalid_state")
        entry = dict(entry_value)
        if entry_is_excluded(entry) or key in active:
            continue
        status = str(entry.get("syncStatus") or "healthy")
        if status in INELIGIBLE_ENTRY_STATUSES or status != "healthy" or entry.get("backlogReason"):
            continue
        if str(entry.get("lastBackup") or "")[:10] == today:
            continue
        channel_id = str(entry.get("channelId") or "")
        relative_path = entry.get("relativePath")
        entry_type = str(entry.get("type") or "")
        if (
            not SNOWFLAKE_RE.fullmatch(channel_id)
            or not isinstance(relative_path, str)
            or entry_type not in {"channel", "thread"}
        ):
            raise DailySyncError("invalid_state")
        _normalized_relative_path(relative_path)
        selected.append((key, entry))
    selected.sort(key=lambda item: (
        str(item[1].get("lastBackup") or ""),
        _normalized_relative_path(str(item[1]["relativePath"])),
        item[0],
    ))
    return selected[:max_entries]


def upsert_queue_item(
    queue: dict[str, Any], key: str, entry: Mapping[str, Any], *, status: str, reason: str
) -> None:
    if reason not in QUEUE_REASONS or status not in {"queued", "retry", "caught_up"}:
        raise DailySyncError("invalid_queue")
    items = queue.setdefault("items", [])
    if not isinstance(items, list):
        raise DailySyncError("invalid_queue")
    timestamp = now_utc()
    payload = {
        "entryKey": key,
        "channelId": entry.get("channelId"),
        "relativePath": entry.get("relativePath", key),
        "type": entry.get("type"),
        "cursorMessageId": newest_cursor(
            entry.get("lastWrittenMessageId"), entry.get("lastMessageId")
        ),
        "priority": 90 if reason in {"rich_full_rebuild_required", "rich_archive_repair_required"} else 60,
        "reason": reason,
        "status": status,
        "updatedAt": timestamp,
    }
    for item in items:
        if not isinstance(item, dict):
            raise DailySyncError("invalid_queue")
        if item.get("entryKey") == key:
            created = item.get("createdAt") or timestamp
            attempts = int(item.get("attempts") or 0)
            item.update(payload)
            item["createdAt"] = created
            item["attempts"] = attempts
            return
    items.append({**payload, "attempts": 0, "createdAt": timestamp})


def plan_mutable_refresh(
    canonical_ids: Sequence[str], scan_cursor: str | None
) -> MutableRefreshPlan:
    ids = [str(value) for value in canonical_ids]
    if any(not SNOWFLAKE_RE.fullmatch(value) for value in ids) or ids != sorted(set(ids), key=int):
        raise DailySyncError("rich_archive_readback_failed")
    if not ids:
        return MutableRefreshPlan(None, None, False)
    cursor_value = snowflake_int(scan_cursor)
    next_id = next((value for value in ids if int(value) > cursor_value), None)
    if next_id is not None:
        return MutableRefreshPlan(next_id, next_id, False)
    return MutableRefreshPlan(ids[0], ids[0], scan_cursor is not None)


def _payload_digest(value: Mapping[str, Any]) -> str:
    return json_sha256(dict(value))


def combine_messages(*groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    digests: dict[str, str] = {}
    for group in groups:
        for value in group:
            if not isinstance(value, Mapping):
                raise DailySyncError("discord_response_invalid")
            message_id = str(value.get("id") or "")
            if not SNOWFLAKE_RE.fullmatch(message_id):
                raise DailySyncError("discord_response_invalid")
            digest = _payload_digest(value)
            if message_id in digests and digests[message_id] != digest:
                raise DailySyncError("discord_duplicate_conflict")
            by_id[message_id] = dict(value)
            digests[message_id] = digest
    return [by_id[key] for key in sorted(by_id, key=int)]


def discord_messages(
    token: str,
    channel_id: str,
    *,
    after: str | None = None,
    around: str | None = None,
    limit: int,
    rate_limit_budget: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    cursor = after if after is not None else around
    if (
        not SNOWFLAKE_RE.fullmatch(channel_id)
        or not isinstance(cursor, str)
        or not SNOWFLAKE_RE.fullmatch(cursor)
        or (after is None) == (around is None)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
    ):
        raise DailySyncError("invalid_discord_identity")
    query = urllib.parse.urlencode({
        "limit": str(limit), "after" if after is not None else "around": cursor
    })
    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages?{query}",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "openclaw-discord-rich-daily/2.0",
        },
    )
    retries = 0
    budget = rate_limit_budget if rate_limit_budget is not None else {"waited": 0.0}
    while True:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except (TypeError, ValueError) as exc:
                        raise DailySyncError("discord_response_invalid") from exc
                    if declared_size < 0 or declared_size > MAX_DISCORD_RESPONSE_BYTES:
                        raise DailySyncError("discord_response_too_large")
                encoded = response.read(MAX_DISCORD_RESPONSE_BYTES + 1)
                if len(encoded) > MAX_DISCORD_RESPONSE_BYTES:
                    raise DailySyncError("discord_response_too_large")
                payload = json.loads(encoded.decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            encoded_error = exc.read(MAX_DISCORD_ERROR_BYTES + 1)
            if exc.code != 429:
                raise DailySyncError("discord_fetch_failed") from exc
            retries += 1
            if retries > MAX_429_RETRIES:
                raise DailySyncError("discord_rate_limit_exhausted") from exc
            try:
                retry_after = float(json.loads(encoded_error.decode("utf-8", errors="ignore")).get("retry_after", 1.0))
            except (TypeError, ValueError, json.JSONDecodeError):
                retry_after = 1.0
            wait_seconds = min(max(retry_after, 0.0) + 0.25, 30.0)
            waited = float(budget.get("waited", 0.0))
            if waited + wait_seconds > MAX_429_WAIT_SECONDS:
                raise DailySyncError("discord_rate_limit_exhausted") from exc
            budget["waited"] = waited + wait_seconds
            time.sleep(wait_seconds)
        except DailySyncError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DailySyncError("discord_fetch_failed") from exc
    if not isinstance(payload, list) or len(payload) > limit:
        raise DailySyncError("discord_response_invalid")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    after_value = snowflake_int(after)
    for value in payload:
        if not isinstance(value, Mapping):
            raise DailySyncError("discord_response_invalid")
        message_id = str(value.get("id") or "")
        message_channel = str(value.get("channel_id") or channel_id)
        if (
            not SNOWFLAKE_RE.fullmatch(message_id)
            or message_id in seen
            or message_channel != channel_id
            or (after is not None and int(message_id) <= after_value)
        ):
            raise DailySyncError("discord_response_invalid")
        seen.add(message_id)
        rows.append(dict(value))
    return sorted(rows, key=lambda row: int(str(row["id"])))


def fetch_new_messages(
    fetch: Callable[..., list[dict[str, Any]]],
    token: str,
    channel_id: str,
    cursor: str,
    *,
    page_size: int,
    max_pages: int,
    max_messages: int,
    remaining_messages: int,
    rate_limit_budget: dict[str, float],
) -> tuple[list[dict[str, Any]], bool, int]:
    rows: list[dict[str, Any]] = []
    after = cursor
    requests = 0
    terminal = False
    for _ in range(max_pages):
        budget = min(page_size, max_messages - len(rows), remaining_messages - len(rows))
        if budget <= 0:
            break
        page = fetch(
            token, channel_id, after=after, limit=budget,
            rate_limit_budget=rate_limit_budget,
        )
        requests += 1
        if not page:
            terminal = True
            break
        combined = combine_messages(rows, page)
        if len(combined) != len(rows) + len(page):
            raise DailySyncError("discord_duplicate_conflict")
        rows = combined
        after = str(rows[-1]["id"])
        if len(page) < budget:
            terminal = True
            break
    return rows, terminal, requests


def fetch_mutable_window(
    fetch: Callable[..., list[dict[str, Any]]],
    token: str,
    channel_id: str,
    plan: MutableRefreshPlan,
    *,
    limit: int,
    canonical_ids: Sequence[str],
    rate_limit_budget: dict[str, float],
) -> tuple[list[dict[str, Any]], int]:
    if plan.target_message_id is None:
        return [], 0
    rows = fetch(
        token, channel_id, around=plan.target_message_id, limit=limit,
        rate_limit_budget=rate_limit_budget,
    )
    allowed = set(canonical_ids)
    return [row for row in rows if str(row.get("id") or "") in allowed], 1


def load_discord_token(config_path: Path, env_name: str) -> str:
    token = os.environ.get(env_name)
    if isinstance(token, str) and token.strip():
        return token.strip()
    config = load_json_object(config_path, "discord_auth_unavailable")
    channels = config.get("channels") if isinstance(config.get("channels"), Mapping) else {}
    discord = channels.get("discord") if isinstance(channels.get("discord"), Mapping) else {}
    token = discord.get("token")
    if not isinstance(token, str) or not token.strip():
        raise DailySyncError("discord_auth_unavailable")
    return token.strip()


def validate_limits(args: argparse.Namespace) -> None:
    bounds = {
        "max_entries": (1, 6),
        "max_write_entries": (1, 4),
        "page_size": (1, 30),
        "max_pages_per_entry": (1, 3),
        "max_messages_per_entry": (1, 60),
        "max_read_messages": (1, 180),
        "mutable_refresh_limit": (1, 30),
    }
    for name, (minimum, maximum) in bounds.items():
        value = getattr(args, name)
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise DailySyncError("invalid_limits")
    if (
        args.max_messages_per_entry < args.page_size
        or args.max_read_messages < args.page_size
        or args.max_read_messages < args.mutable_refresh_limit
    ):
        raise DailySyncError("invalid_limits")


def execute(
    args: argparse.Namespace,
    *,
    fetch: Callable[..., list[dict[str, Any]]] = discord_messages,
    rich_factory: RichSlotFactory | None = None,
) -> tuple[dict[str, Any], int]:
    """Validate non-core inputs, then stop until the reviewed v2 port exists."""
    del fetch
    validate_limits(args)
    state_path = safe_path(args.state, require_file=True)
    queue_path = safe_path(args.queue, require_file=True)
    inventory_path = safe_path(args.inventory, require_file=True)
    mapping_path = safe_path(args.mapping_ledger, require_file=True)
    safe_path(args.root, require_directory=True)
    safe_path(args.openclaw_config, require_file=True)
    state = load_json_object(state_path, "unreadable_state")
    queue = load_json_object(queue_path, "unreadable_queue")
    inventory = load_json_object(inventory_path, "unreadable_inventory")
    mapping = load_json_object(mapping_path, "unreadable_mapping_ledger")
    binding = validate_inventory_binding(
        inventory, mapping, guild_id=args.guild_id, today=args.today,
        timezone_name=args.timezone,
    )
    selected = select_candidates(state, queue, today=args.today, max_entries=args.max_entries)
    if rich_factory is None:
        raise DailySyncError("rich_core_contract_pending")
    # The orchestration call is deliberately deferred.  Keeping the port unused
    # prevents this scaffold from silently accepting the independently blocked
    # token/evidence implementation.
    return {
        "ok": False,
        "status": "blocked",
        "reason": "rich_core_contract_pending",
        "inventoryDigest": binding["inventoryDigest"],
        "selected": len(selected),
    }, 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one bounded deterministic daily Discord rich-sync slot."
    )
    parser.add_argument("--role", required=True, choices=("daily-sync-1", "daily-sync-2", "daily-sync-3"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--mapping-ledger", required=True)
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--today", required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--openclaw-config", required=True)
    parser.add_argument("--token-env", default="DISCORD_BOT_TOKEN")
    parser.add_argument("--max-entries", type=int, default=6)
    parser.add_argument("--max-write-entries", type=int, default=4)
    parser.add_argument("--page-size", type=int, default=30)
    parser.add_argument("--max-pages-per-entry", type=int, default=2)
    parser.add_argument("--max-messages-per-entry", type=int, default=60)
    parser.add_argument("--max-read-messages", type=int, default=180)
    parser.add_argument("--mutable-refresh-limit", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result, code = execute(parse_args(argv))
    except DailySyncError as exc:
        result, code = {"ok": False, "status": "error", "reason": exc.category}, 2
    except Exception:
        result, code = {"ok": False, "status": "error", "reason": "unexpected_error"}, 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
