#!/usr/bin/env python3
"""Bounded daily Discord rich-sync runner using the managed Adapter V3.

The production CLI loads exactly the sibling adapter and rich core pinned by
the installed runtime manifest. Persisted Mapping receipts are audit-only;
only process-local capabilities issued while the canonical archive lock is
held may authorize a state or cursor update.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


INVENTORY_BINDING_SCHEMA = "openclaw-discord-daily-inventory-binding.v2"
ENTRY_BINDING_SCHEMA = "openclaw-discord-daily-entry-binding.v2"
CURRENT_SNAPSHOT_SCHEMA = "openclaw-discord-rich-current-snapshot.v2"
MERGE_COMMIT_SCHEMA = "openclaw-discord-rich-incremental-commit.v2"
RICH_CORE_ADAPTER_VERSION = "openclaw-discord-rich-core-adapter.v3"
RUNTIME_MANIFEST_SCHEMA = "openclaw-discord-runtime-components.v1"
RUNTIME_MANIFEST_RELATIVE = "manifests/runtime-components.v1.json"
MANAGED_ADAPTER_FILENAME = "rich_core_adapter_v3.py"
MANAGED_RUNNER_FILENAME = "run_daily_sync_v3.py"
MANAGED_DISPATCHER_FILENAME = "run_managed_component.py"
RUNTIME_COMPONENT_PATHS = {
    "rich_message_archive.py": "scripts/rich_message_archive.py",
    MANAGED_ADAPTER_FILENAME: f"scripts/{MANAGED_ADAPTER_FILENAME}",
    MANAGED_RUNNER_FILENAME: f"scripts/{MANAGED_RUNNER_FILENAME}",
    MANAGED_DISPATCHER_FILENAME: f"scripts/{MANAGED_DISPATCHER_FILENAME}",
}
CONFIG_BINDING_VIEW_KEYS = frozenset({
    "schemaVersion",
    "authority",
    "configSha256",
    "configPathSha256",
    "workspaceRootSha256",
    "archiveRootSha256",
    "guildId",
    "timezone",
    "statePathSha256",
    "queuePathSha256",
    "openclawConfigPathSha256",
    "dailyEntryLimit",
    "dailyMessageLimit",
    "dailyMutableRefreshLimit",
})
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
    "rich_core_load_failed",
    "rich_core_integrity_mismatch",
    "rich_core_contract_unsupported",
    "rich_core_authority_invalid",
    "rich_core_authority_expired",
    "rich_baseline_missing",
    "rich_archive_merge_failed",
    "rich_archive_readback_failed",
    "rich_cursor_not_authorized",
    "rich_asset_budget_exhausted",
    "rich_full_stage_failed",
    "rich_full_evidence_failed",
    "rich_full_run_incomplete",
    "rich_root_publish_failed",
    "rich_root_readback_failed",
    "rich_root_rollback_failed",
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


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_DIR.parent
_RUNTIME_MANIFEST = _SKILL_ROOT / RUNTIME_MANIFEST_RELATIVE
_MANAGED_ADAPTER_CACHE: tuple[tuple[Any, ...], ModuleType] | None = None
MAX_RUNTIME_COMPONENT_BYTES = 8 * 1024 * 1024


def _runtime_component_row(
    payload: Mapping[str, Any], filename: str
) -> dict[str, Any]:
    components = payload.get("components")
    row = components.get(filename) if isinstance(components, Mapping) else None
    if (
        not isinstance(row, dict)
        or set(row) != {"relativePath", "sha256", "mode", "owner", "links"}
        or filename not in RUNTIME_COMPONENT_PATHS
        or row.get("relativePath") != RUNTIME_COMPONENT_PATHS[filename]
        or row.get("mode") != "0600"
        or row.get("owner") != "effective-user"
        or row.get("links") != 1
        or not HASH_RE.fullmatch(str(row.get("sha256") or ""))
    ):
        raise DailySyncError("rich_core_integrity_mismatch")
    return row


def _read_runtime_component(
    path: Path, row: Mapping[str, Any]
) -> tuple[bytes, tuple[Any, ...]]:
    reject_symlink_components(path)
    descriptor = -1
    try:
        before = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError as exc:
        raise DailySyncError("rich_core_integrity_mismatch") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        if descriptor >= 0:
            os.close(descriptor)
        raise DailySyncError("rich_core_integrity_mismatch")
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RUNTIME_COMPONENT_BYTES:
                raise DailySyncError("rich_core_integrity_mismatch")
            chunks.append(chunk)
        after = path.lstat()
    except (OSError, DailySyncError) as exc:
        if isinstance(exc, DailySyncError):
            raise
        raise DailySyncError("rich_core_integrity_mismatch") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    encoded = b"".join(chunks)
    if (
        len(encoded) != info.st_size
        or hashlib.sha256(encoded).hexdigest() != row.get("sha256")
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    ):
        raise DailySyncError("rich_core_integrity_mismatch")
    return encoded, (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, row["sha256"]
    )


def _verify_runtime_component(path: Path, row: Mapping[str, Any]) -> tuple[Any, ...]:
    _encoded, identity = _read_runtime_component(path, row)
    return identity


def load_managed_rich_adapter() -> ModuleType:
    """Load only the manifest-pinned sibling Adapter V3 module."""
    global _MANAGED_ADAPTER_CACHE
    manifest_path = Path(os.path.abspath(_RUNTIME_MANIFEST))
    reject_symlink_components(manifest_path)
    descriptor = -1
    try:
        before = manifest_path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(manifest_path, flags)
        manifest_info = os.fstat(descriptor)
        encoded = os.read(descriptor, 64 * 1024 + 1)
        after = manifest_path.lstat()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DailySyncError("rich_core_integrity_mismatch") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        len(encoded) > 64 * 1024
        or len(encoded) != manifest_info.st_size
        or not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(manifest_info.st_mode)
        or (before.st_dev, before.st_ino)
        != (manifest_info.st_dev, manifest_info.st_ino)
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (
            manifest_info.st_dev,
            manifest_info.st_ino,
            manifest_info.st_size,
            manifest_info.st_mtime_ns,
        )
        or manifest_info.st_uid != os.geteuid()
        or manifest_info.st_nlink != 1
        or stat.S_IMODE(manifest_info.st_mode) != 0o600
        or not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "adapterContract", "components"}
        or payload.get("schemaVersion") != RUNTIME_MANIFEST_SCHEMA
        or payload.get("adapterContract") != RICH_CORE_ADAPTER_VERSION
        or not isinstance(payload.get("components"), dict)
        or set(payload["components"]) != set(RUNTIME_COMPONENT_PATHS)
    ):
        raise DailySyncError("rich_core_integrity_mismatch")
    rows = {
        filename: _runtime_component_row(payload, filename)
        for filename in RUNTIME_COMPONENT_PATHS
    }
    expected_hashes = {
        filename: str(row["sha256"])
        for filename, row in rows.items()
    }
    for filename, row in rows.items():
        if filename != MANAGED_ADAPTER_FILENAME:
            _verify_runtime_component(_SCRIPT_DIR / filename, row)
    runner_row = rows[MANAGED_RUNNER_FILENAME]
    adapter_row = rows[MANAGED_ADAPTER_FILENAME]
    adapter_source, cache_key = _read_runtime_component(
        _SCRIPT_DIR / MANAGED_ADAPTER_FILENAME, adapter_row
    )
    if _MANAGED_ADAPTER_CACHE is not None and _MANAGED_ADAPTER_CACHE[0] == cache_key:
        cached_module = _MANAGED_ADAPTER_CACHE[1]
        try:
            cached_hashes = cached_module.validate_runtime_components()
        except Exception as exc:
            category = getattr(exc, "category", "rich_core_integrity_mismatch")
            raise DailySyncError(category) from exc
        if cached_hashes != expected_hashes:
            raise DailySyncError("rich_core_integrity_mismatch")
        return cached_module
    module_name = f"_openclaw_managed_rich_adapter_{adapter_row['sha256'][:20]}"
    module = ModuleType(module_name)
    module.__file__ = str(_SCRIPT_DIR / MANAGED_ADAPTER_FILENAME)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        compiled = compile(
            adapter_source,
            str(_SCRIPT_DIR / MANAGED_ADAPTER_FILENAME),
            "exec",
            dont_inherit=True,
        )
        exec(compiled, module.__dict__)
        component_hashes = module.validate_runtime_components()
    except Exception as exc:
        sys.modules.pop(module_name, None)
        category = getattr(exc, "category", "rich_core_load_failed")
        raise DailySyncError(category) from exc
    if (
        getattr(module, "ADAPTER_VERSION", None) != RICH_CORE_ADAPTER_VERSION
        or type(getattr(module, "ManagedRichCoreAdapterV3", None)) is not type
        or component_hashes != expected_hashes
    ):
        raise DailySyncError("rich_core_contract_unsupported")
    _MANAGED_ADAPTER_CACHE = (cache_key, module)
    return module


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


CURRENT_SNAPSHOT_KEYS = frozenset({
    "schemaVersion",
    "entryBindingSha256",
    "channelId",
    "generationId",
    "generationSha256",
    "pointerSha256",
    "canonicalMessageIds",
    "activeApiSourcePayloadSha256ById",
    "localGateStatus",
    "fullEvidenceGateStatus",
    "verifiedEmpty",
})
MERGE_COMMIT_KEYS = frozenset({
    "schemaVersion",
    "entryBindingSha256",
    "channelId",
    "preCurrentGenerationId",
    "preCurrentGenerationSha256",
    "committedGenerationId",
    "committedGenerationSha256",
    "currentPointerSha256",
    "fetchedMessageIds",
    "fetchedRawPayloadSha256ById",
    "activeApiSourcePayloadSha256ById",
    "inventoryDigest",
    "inventoryObservedAt",
    "verifiedCutoff",
    "runContextId",
    "lockReceiptSha256",
    "budgetReceiptSha256",
    "mode",
})


def _safe_receipt_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 160
        and not any(unicodedata.category(character) == "Cc" for character in value)
    )


def _exact_hash_map(value: Any, expected_ids: Sequence[str]) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != set(expected_ids):
        return None
    normalized = dict(value)
    if any(
        not isinstance(item, str) or not HASH_RE.fullmatch(item)
        for item in normalized.values()
    ):
        return None
    return normalized


def validate_current_snapshot(
    snapshot: Mapping[str, Any], entry_binding: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate one exact CURRENT readback; generic success flags are ignored."""
    if not isinstance(snapshot, Mapping) or set(snapshot) != CURRENT_SNAPSHOT_KEYS:
        raise DailySyncError("rich_archive_readback_failed")
    message_ids = snapshot.get("canonicalMessageIds")
    source_hashes = snapshot.get("activeApiSourcePayloadSha256ById")
    generation_id = snapshot.get("generationId")
    if (
        snapshot.get("schemaVersion") != CURRENT_SNAPSHOT_SCHEMA
        or snapshot.get("entryBindingSha256") != entry_binding.get("entryBindingSha256")
        or snapshot.get("channelId") != entry_binding.get("channelId")
        or not isinstance(snapshot.get("entryBindingSha256"), str)
        or not HASH_RE.fullmatch(snapshot["entryBindingSha256"])
        or not isinstance(snapshot.get("channelId"), str)
        or not SNOWFLAKE_RE.fullmatch(snapshot["channelId"])
        or not _safe_receipt_identifier(generation_id)
        or not isinstance(snapshot.get("generationSha256"), str)
        or not HASH_RE.fullmatch(snapshot["generationSha256"])
        or not isinstance(snapshot.get("pointerSha256"), str)
        or not HASH_RE.fullmatch(snapshot["pointerSha256"])
        or not isinstance(message_ids, list)
        or not isinstance(source_hashes, Mapping)
        or snapshot.get("localGateStatus") != "PASS"
        or snapshot.get("fullEvidenceGateStatus") not in {"PASS", "NOT_PROVIDED"}
        or type(snapshot.get("verifiedEmpty")) is not bool
    ):
        raise DailySyncError("rich_archive_readback_failed")
    normalized_ids = [str(value) for value in message_ids]
    if (
        any(not SNOWFLAKE_RE.fullmatch(value) for value in normalized_ids)
        or normalized_ids != sorted(set(normalized_ids), key=int)
        or _exact_hash_map(source_hashes, normalized_ids) is None
        or (snapshot["verifiedEmpty"] and normalized_ids)
        or (
            snapshot["verifiedEmpty"]
            and snapshot["fullEvidenceGateStatus"] != "PASS"
        )
    ):
        raise DailySyncError("rich_archive_readback_failed")
    return {
        **dict(snapshot),
        "canonicalMessageIds": normalized_ids,
        "activeApiSourcePayloadSha256ById": dict(source_hashes),
    }


def validate_merge_readback(
    commit: Mapping[str, Any],
    current_snapshot: Mapping[str, Any],
    *,
    entry_binding: Mapping[str, Any],
    expected_raw_sha256_by_id: Mapping[str, str],
    expected_pre_current: Mapping[str, Any],
    expected_run_context_id: str,
    expected_lock_receipt_sha256: str,
    expected_budget_receipt_sha256: str,
    inventory_digest: str,
    inventory_observed_at: str,
    verified_cutoff: str,
) -> dict[str, Any]:
    """Bind an incremental commit to the exact fetched payloads and CURRENT.

    The commit and the independently inspected CURRENT are both exact schemas;
    generic success flags, omitted IDs, stale pointers, or substituted source
    digests fail closed before any caller may advance canonical state.
    """
    if not isinstance(commit, Mapping) or set(commit) != MERGE_COMMIT_KEYS:
        raise DailySyncError("rich_archive_readback_failed")
    pre_current = validate_current_snapshot(expected_pre_current, entry_binding)
    if (
        not _safe_receipt_identifier(expected_run_context_id)
        or not isinstance(expected_lock_receipt_sha256, str)
        or not HASH_RE.fullmatch(expected_lock_receipt_sha256)
        or not isinstance(expected_budget_receipt_sha256, str)
        or not HASH_RE.fullmatch(expected_budget_receipt_sha256)
    ):
        raise DailySyncError("rich_archive_readback_failed")
    fetched_ids = commit.get("fetchedMessageIds")
    if not isinstance(fetched_ids, list):
        raise DailySyncError("rich_archive_readback_failed")
    normalized_ids = [str(value) for value in fetched_ids]
    if not isinstance(expected_raw_sha256_by_id, Mapping):
        raise DailySyncError("rich_archive_readback_failed")
    expected_input_ids = [str(value) for value in expected_raw_sha256_by_id]
    if (
        any(not SNOWFLAKE_RE.fullmatch(value) for value in expected_input_ids)
        or len(expected_input_ids) != len(set(expected_input_ids))
    ):
        raise DailySyncError("rich_archive_readback_failed")
    expected_ids = sorted(expected_input_ids, key=int)
    raw_hashes = _exact_hash_map(
        commit.get("fetchedRawPayloadSha256ById"), normalized_ids
    )
    source_hashes = _exact_hash_map(
        commit.get("activeApiSourcePayloadSha256ById"), normalized_ids
    )
    expected_raw_hashes = _exact_hash_map(expected_raw_sha256_by_id, expected_ids)
    if (
        commit.get("schemaVersion") != MERGE_COMMIT_SCHEMA
        or commit.get("mode") != "incremental"
        or commit.get("entryBindingSha256")
        != entry_binding.get("entryBindingSha256")
        or commit.get("channelId") != entry_binding.get("channelId")
        or any(not SNOWFLAKE_RE.fullmatch(value) for value in normalized_ids)
        or normalized_ids != sorted(set(normalized_ids), key=int)
        or normalized_ids != expected_ids
        or raw_hashes is None
        or expected_raw_hashes is None
        or raw_hashes != expected_raw_hashes
        or source_hashes is None
        or inventory_digest != entry_binding.get("inventoryDigest")
        or inventory_observed_at != entry_binding.get("inventoryObservedAt")
        or not isinstance(inventory_digest, str)
        or not HASH_RE.fullmatch(inventory_digest)
        or commit.get("inventoryDigest") != inventory_digest
        or commit.get("inventoryObservedAt") != inventory_observed_at
        or commit.get("verifiedCutoff") != verified_cutoff
        or commit.get("preCurrentGenerationId") != pre_current["generationId"]
        or commit.get("preCurrentGenerationSha256")
        != pre_current["generationSha256"]
        or not _safe_receipt_identifier(commit.get("committedGenerationId"))
        or commit.get("runContextId") != expected_run_context_id
        or commit.get("lockReceiptSha256") != expected_lock_receipt_sha256
        or commit.get("budgetReceiptSha256") != expected_budget_receipt_sha256
        or any(
            not isinstance(commit.get(key), str)
            or not HASH_RE.fullmatch(commit[key])
            for key in (
                "preCurrentGenerationSha256",
                "committedGenerationSha256",
                "currentPointerSha256",
                "lockReceiptSha256",
                "budgetReceiptSha256",
            )
        )
    ):
        raise DailySyncError("rich_archive_readback_failed")

    current = validate_current_snapshot(current_snapshot, entry_binding)
    current_ids = set(current["canonicalMessageIds"])
    current_sources = current["activeApiSourcePayloadSha256ById"]
    if (
        commit["committedGenerationId"] != current["generationId"]
        or commit["committedGenerationSha256"] != current["generationSha256"]
        or commit["currentPointerSha256"] != current["pointerSha256"]
        or not set(normalized_ids).issubset(current_ids)
        or any(current_sources.get(key) != source_hashes[key] for key in normalized_ids)
    ):
        raise DailySyncError("rich_archive_readback_failed")
    return {
        **dict(commit),
        "fetchedMessageIds": normalized_ids,
        "fetchedRawPayloadSha256ById": raw_hashes,
        "activeApiSourcePayloadSha256ById": source_hashes,
    }


def validate_quiet_current(
    snapshot: Mapping[str, Any], entry_binding: Mapping[str, Any], *, cursor: str | None
) -> dict[str, Any]:
    """A quiet API result may update dates only when CURRENT proves its baseline."""
    current = validate_current_snapshot(snapshot, entry_binding)
    canonical_ids = current["canonicalMessageIds"]
    if cursor is None:
        if canonical_ids or current["verifiedEmpty"] is not True:
            raise DailySyncError("rich_baseline_missing")
        return current
    if not SNOWFLAKE_RE.fullmatch(cursor) or cursor not in canonical_ids:
        raise DailySyncError("rich_archive_readback_failed")
    return current


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
    head: bool = False,
    limit: int,
    rate_limit_budget: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    cursor = after if after is not None else around
    mode_count = int(after is not None) + int(around is not None) + int(head is True)
    if (
        not SNOWFLAKE_RE.fullmatch(channel_id)
        or type(head) is not bool
        or mode_count != 1
        or (not head and (not isinstance(cursor, str) or not SNOWFLAKE_RE.fullmatch(cursor)))
        or (head and cursor is not None)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
    ):
        raise DailySyncError("invalid_discord_identity")
    query_values = {"limit": str(limit)}
    if not head:
        query_values["after" if after is not None else "around"] = str(cursor)
    query = urllib.parse.urlencode(query_values)
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
) -> tuple[list[dict[str, Any]], int, int]:
    if plan.target_message_id is None:
        return [], 0, 0
    rows = fetch(
        token, channel_id, around=plan.target_message_id, limit=limit,
        rate_limit_budget=rate_limit_budget,
    )
    allowed = set(canonical_ids)
    return (
        [row for row in rows if str(row.get("id") or "") in allowed],
        1,
        len(rows),
    )


def fetch_verified_empty_head(
    fetch: Callable[..., list[dict[str, Any]]],
    token: str,
    channel_id: str,
    *,
    snapshot: Mapping[str, Any],
    entry_binding: Mapping[str, Any],
    limit: int,
    rate_limit_budget: dict[str, float],
) -> tuple[list[dict[str, Any]], bool, int]:
    """Bounded first-message probe authorized only by a verified empty CURRENT.

    Discord's cursor-less endpoint returns a bounded head page.  Only an
    explicit empty response proves completion; a non-empty response is retained
    but remains partial regardless of its length.
    """
    current = validate_quiet_current(snapshot, entry_binding, cursor=None)
    if current["verifiedEmpty"] is not True or current["canonicalMessageIds"]:
        raise DailySyncError("rich_baseline_missing")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise DailySyncError("invalid_limits")
    page = fetch(
        token,
        channel_id,
        head=True,
        limit=limit,
        rate_limit_budget=rate_limit_budget,
    )
    rows = combine_messages(page)
    if len(rows) != len(page) or any(
        str(row.get("channel_id") or channel_id) != channel_id for row in rows
    ):
        raise DailySyncError("discord_response_invalid")
    return rows, not rows, 1


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


def _translate_adapter_error(exc: BaseException) -> DailySyncError:
    return DailySyncError(str(getattr(exc, "category", "rich_core_contract_unsupported")))


def _persist_progress(
    *,
    queue_path: Path,
    queue: dict[str, Any],
    state_path: Path,
    state: dict[str, Any],
) -> None:
    timestamp = now_utc()
    queue["updatedAt"] = timestamp
    state["updatedAt"] = timestamp
    persist_queue_then_state(queue_path, queue, state_path, state)


def _queue_for_rebuild(
    queue: dict[str, Any], key: str, entry: dict[str, Any]
) -> None:
    entry.update({
        "syncStatus": "queued",
        "backlogReason": "rich_full_rebuild_required",
        "richArchiveIncrementalStatus": "full_rebuild_required",
    })
    upsert_queue_item(
        queue,
        key,
        entry,
        status="queued",
        reason="rich_full_rebuild_required",
    )


def _queue_for_retry(
    queue: dict[str, Any], key: str, entry: dict[str, Any], *, reason: str
) -> None:
    if reason not in {
        "rich_incremental_read_error",
        "rich_incremental_merge_error",
        "rich_incremental_readback_error",
    }:
        reason = "rich_incremental_readback_error"
    entry.update({
        "syncStatus": "retry",
        "backlogReason": reason,
        "richArchiveIncrementalStatus": "retry",
        "consecutiveErrors": int(entry.get("consecutiveErrors") or 0) + 1,
    })
    upsert_queue_item(queue, key, entry, status="retry", reason=reason)


def _apply_consumed_grant(
    entry: dict[str, Any],
    grant: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    config_binding: Mapping[str, Any],
    today: str,
    partial: bool,
    mutable_plan: MutableRefreshPlan | None,
) -> None:
    if (
        grant.get("authority") != "CONSUMED_RUNTIME_GRANT"
        or grant.get("entryBindingSha256") != binding.get("entryBindingSha256")
        or grant.get("channelId") != binding.get("channelId")
        or grant.get("inventoryDigest") != binding.get("inventoryDigest")
        or grant.get("inventoryObservedAt") != binding.get("inventoryObservedAt")
        or grant.get("configSha256") != config_binding.get("configSha256")
        or grant.get("configPathSha256")
        != config_binding.get("configPathSha256")
        or not isinstance(grant.get("generationId"), str)
        or not HASH_RE.fullmatch(str(grant.get("generationSha256") or ""))
        or not HASH_RE.fullmatch(str(grant.get("pointerSha256") or ""))
    ):
        raise DailySyncError("rich_archive_readback_failed")
    cursor = grant.get("authorizedCursor")
    if cursor is not None and not SNOWFLAKE_RE.fullmatch(str(cursor)):
        raise DailySyncError("rich_cursor_not_authorized")
    observed_at = str(grant.get("observedAt") or now_utc())
    entry.update({
        "syncStatus": "partial" if partial else "healthy",
        "backlogReason": "rich_incremental_partial" if partial else None,
        "richArchiveIncrementalStatus": "verified",
        "richArchiveIncrementalGenerationId": grant["generationId"],
        "richArchiveIncrementalVerifiedAt": observed_at,
        "consecutiveErrors": 0,
    })
    if cursor is not None:
        entry["lastWrittenMessageId"] = str(cursor)
        entry["lastMessageId"] = str(cursor)
    if grant.get("quiet") is not True:
        entry["lastSuccessfulWriteAt"] = observed_at
    if mutable_plan is not None and mutable_plan.next_scan_cursor is not None:
        entry["richMutableScanCursor"] = mutable_plan.next_scan_cursor
        if mutable_plan.cycle_completed:
            entry["richMutableScanCompletedAt"] = observed_at
    if not partial:
        entry["lastBackup"] = today


def execute(
    args: argparse.Namespace,
    *,
    fetch: Callable[..., list[dict[str, Any]]] = discord_messages,
) -> tuple[dict[str, Any], int]:
    """Run one bounded slot through opaque core-issued capabilities."""
    validate_limits(args)
    state_path = safe_path(args.state, require_file=True)
    queue_path = safe_path(args.queue, require_file=True)
    inventory_path = safe_path(args.inventory, require_file=True)
    mapping_path = safe_path(args.mapping_ledger, require_file=True)
    archive_root = safe_path(args.root, require_directory=True)
    workspace_root = safe_path(args.workspace, require_directory=True)
    backup_config_path = safe_path(args.backup_config, require_file=True)
    config_path = safe_path(args.openclaw_config, require_file=True)
    adapter_module = load_managed_rich_adapter()
    try:
        adapter = adapter_module.ManagedRichCoreAdapterV3()
        slot_manager = adapter.open_slot(
            archive_root=archive_root,
            lock_path=archive_root / adapter_module.CANONICAL_LOCK_NAME,
            workspace_root=workspace_root,
            config_path=backup_config_path,
        )
        with slot_manager as slot:
            if type(slot) is not adapter_module.LockedRichSlotV3:
                raise DailySyncError("rich_core_contract_unsupported")
            try:
                config_binding = slot.config_view()
            except adapter_module.RichCoreAdapterError as exc:
                raise _translate_adapter_error(exc) from exc
            if (
                set(config_binding) != CONFIG_BINDING_VIEW_KEYS
                or config_binding.get("schemaVersion")
                != adapter_module.CONFIG_BINDING_AUDIT_SCHEMA
                or config_binding.get("authority") != "AUDIT_ONLY"
                or config_binding.get("guildId") != args.guild_id
                or config_binding.get("timezone") != args.timezone
                or config_binding.get("statePathSha256")
                != json_sha256(str(state_path))
                or config_binding.get("queuePathSha256")
                != json_sha256(str(queue_path))
                or config_binding.get("openclawConfigPathSha256")
                != json_sha256(str(config_path))
                or config_binding.get("dailyEntryLimit") != args.max_entries
                or config_binding.get("dailyMessageLimit")
                != args.max_messages_per_entry
                or config_binding.get("dailyMutableRefreshLimit")
                != args.mutable_refresh_limit
                or not HASH_RE.fullmatch(
                    str(config_binding.get("configSha256") or "")
                )
                or not HASH_RE.fullmatch(
                    str(config_binding.get("configPathSha256") or "")
                )
            ):
                raise DailySyncError("rich_core_authority_invalid")

            # Mutable state and queue are first opened only after the canonical
            # archive-root lock is held by the reviewed core.
            state = load_json_object(state_path, "unreadable_state")
            queue = load_json_object(queue_path, "unreadable_queue")
            inventory = load_json_object(inventory_path, "unreadable_inventory")
            mapping = load_json_object(mapping_path, "unreadable_mapping_ledger")
            inventory_binding = validate_inventory_binding(
                inventory,
                mapping,
                guild_id=args.guild_id,
                today=args.today,
                timezone_name=args.timezone,
            )
            selected = select_candidates(
                state, queue, today=args.today, max_entries=args.max_entries
            )
            if not selected:
                return {
                    "ok": True,
                    "status": "ok",
                    "checked": 0,
                    "writtenEntries": 0,
                    "writtenMessages": 0,
                    "refreshedMessages": 0,
                    "mergedMessages": 0,
                    "queued": 0,
                    "totalRead": 0,
                    "activeQueueLeft": sum(
                        1 for item in queue.get("items", [])
                        if isinstance(item, Mapping)
                        and item.get("status") in ACTIVE_QUEUE_STATUSES
                    ),
                }, 0

            audit_bindings = tuple(
                bind_entry_inventory(
                    inventory_binding,
                    channel_id=str(entry["channelId"]),
                    relative_path=str(entry["relativePath"]),
                    entry_type=str(entry["type"]),
                )
                for _key, entry in selected
            )
            rich_bindings = tuple(
                adapter_module.EntryBindingV3.from_mapping(row)
                for row in audit_bindings
            )
            begin_request = adapter_module.IncrementalBeginRequestV3(
                schema_version=adapter_module.INCREMENTAL_BEGIN_SCHEMA,
                role=args.role,
                archive_root=archive_root,
                workspace_root=workspace_root,
                state_path=state_path,
                queue_path=queue_path,
                openclaw_config_path=config_path,
                timezone_name=args.timezone,
                inventory_digest=str(inventory_binding["inventoryDigest"]),
                inventory_observed_at=str(inventory_binding["observedAt"]),
                entry_bindings=rich_bindings,
                limits=(
                    ("maxEntries", args.max_entries),
                    ("maxWriteEntries", args.max_write_entries),
                    ("maxReadMessages", args.max_read_messages),
                    ("maxMessagesPerEntry", args.max_messages_per_entry),
                    ("mutableRefreshLimit", args.mutable_refresh_limit),
                ),
            )
            token = load_discord_token(config_path, args.token_env)
            rate_limit_budget: dict[str, float] = {"waited": 0.0}
            checked = 0
            written_entries = 0
            written_messages = 0
            refreshed_messages = 0
            merged_messages = 0
            queued = 0
            total_read = 0

            with slot.begin_incremental(begin_request) as session:
                if type(session) is not adapter_module.IncrementalSessionV3:
                    raise DailySyncError("rich_core_contract_unsupported")
                for (key, selected_entry), audit_binding, rich_binding in zip(
                    selected, audit_bindings, rich_bindings
                ):
                    if (
                        written_entries >= args.max_write_entries
                        or total_read >= args.max_read_messages
                    ):
                        break
                    entries = state.get("entries")
                    live_entry = entries.get(key) if isinstance(entries, dict) else None
                    if not isinstance(live_entry, dict) or dict(live_entry) != selected_entry:
                        raise DailySyncError("invalid_state")
                    checked += 1
                    try:
                        current = session.inspect_current(rich_binding)
                        current_view = session.current_view(current)
                    except adapter_module.RichCoreAdapterError as exc:
                        if exc.category == "rich_baseline_missing":
                            _queue_for_rebuild(queue, key, live_entry)
                            queued += 1
                            _persist_progress(
                                queue_path=queue_path, queue=queue,
                                state_path=state_path, state=state,
                            )
                            continue
                        _queue_for_retry(
                            queue, key, live_entry,
                            reason="rich_incremental_readback_error",
                        )
                        _persist_progress(
                            queue_path=queue_path, queue=queue,
                            state_path=state_path, state=state,
                        )
                        raise

                    cursor = newest_cursor(
                        live_entry.get("lastWrittenMessageId"),
                        live_entry.get("lastMessageId"),
                    )
                    mutable_plan: MutableRefreshPlan | None = None
                    if cursor is None:
                        def head_fetch(channel_id: str, limit: int):
                            return fetch(
                                token, channel_id, head=True, limit=limit,
                                rate_limit_budget=rate_limit_budget,
                            )

                        try:
                            probe = session.probe_head(
                                rich_binding,
                                fetch_page=head_fetch,
                                limit=min(args.page_size, args.max_read_messages - total_read),
                            )
                            probe_view = session.head_probe_view(probe)
                        except adapter_module.RichCoreAdapterError as exc:
                            if exc.category == "rich_baseline_missing":
                                _queue_for_rebuild(queue, key, live_entry)
                                queued += 1
                                _persist_progress(
                                    queue_path=queue_path, queue=queue,
                                    state_path=state_path, state=state,
                                )
                                continue
                            _queue_for_retry(
                                queue, key, live_entry,
                                reason="rich_incremental_read_error",
                            )
                            _persist_progress(
                                queue_path=queue_path, queue=queue,
                                state_path=state_path, state=state,
                            )
                            raise
                        total_read += len(probe_view["messageIds"])
                        if probe_view["messageIds"]:
                            _queue_for_rebuild(queue, key, live_entry)
                            queued += 1
                            _persist_progress(
                                queue_path=queue_path, queue=queue,
                                state_path=state_path, state=state,
                            )
                            continue
                        try:
                            grant = session.authorize_quiet_update(
                                current,
                                requested_cursor=None,
                                head_probe=probe,
                            )
                            consumed = session.consume_state_update_grant(grant)
                        except adapter_module.RichCoreAdapterError:
                            _queue_for_retry(
                                queue, key, live_entry,
                                reason="rich_incremental_readback_error",
                            )
                            _persist_progress(
                                queue_path=queue_path, queue=queue,
                                state_path=state_path, state=state,
                            )
                            raise
                        _apply_consumed_grant(
                            live_entry,
                            consumed,
                            binding=audit_binding,
                            config_binding=config_binding,
                            today=args.today,
                            partial=False,
                            mutable_plan=None,
                        )
                        _persist_progress(
                            queue_path=queue_path, queue=queue,
                            state_path=state_path, state=state,
                        )
                        continue

                    canonical_ids = list(current_view["canonicalMessageIds"])
                    if cursor not in canonical_ids:
                        _queue_for_rebuild(queue, key, live_entry)
                        queued += 1
                        _persist_progress(
                            queue_path=queue_path, queue=queue,
                            state_path=state_path, state=state,
                        )
                        continue
                    try:
                        new_messages, terminal, _requests = fetch_new_messages(
                            fetch,
                            token,
                            str(live_entry["channelId"]),
                            cursor,
                            page_size=args.page_size,
                            max_pages=args.max_pages_per_entry,
                            max_messages=args.max_messages_per_entry,
                            remaining_messages=args.max_read_messages - total_read,
                            rate_limit_budget=rate_limit_budget,
                        )
                    except DailySyncError:
                        _queue_for_retry(
                            queue, key, live_entry,
                            reason="rich_incremental_read_error",
                        )
                        _persist_progress(
                            queue_path=queue_path, queue=queue,
                            state_path=state_path, state=state,
                        )
                        raise
                    total_read += len(new_messages)
                    mutable_plan = plan_mutable_refresh(
                        canonical_ids, live_entry.get("richMutableScanCursor")
                    )
                    refresh_messages: list[dict[str, Any]] = []
                    remaining = args.max_read_messages - total_read
                    if remaining > 0 and mutable_plan.target_message_id is not None:
                        try:
                            (
                                refresh_messages,
                                _refresh_requests,
                                mutable_rows_read,
                            ) = fetch_mutable_window(
                                fetch,
                                token,
                                str(live_entry["channelId"]),
                                mutable_plan,
                                limit=min(args.mutable_refresh_limit, remaining),
                                canonical_ids=canonical_ids,
                                rate_limit_budget=rate_limit_budget,
                            )
                        except DailySyncError:
                            _queue_for_retry(
                                queue, key, live_entry,
                                reason="rich_incremental_read_error",
                            )
                            _persist_progress(
                                queue_path=queue_path, queue=queue,
                                state_path=state_path, state=state,
                            )
                            raise
                        # Account for every Discord row returned, including
                        # around-window rows filtered out of the merge set.
                        total_read += mutable_rows_read
                    messages = combine_messages(refresh_messages, new_messages)
                    partial = not terminal
                    if messages:
                        new_ids = tuple(
                            sorted(
                                {str(row["id"]) for row in new_messages},
                                key=int,
                            )
                        )
                        merge_request = adapter_module.IncrementalMergeRequestV3(
                            schema_version=adapter_module.INCREMENTAL_MERGE_SCHEMA,
                            entry=rich_binding,
                            observed_at=now_utc(),
                            messages=tuple(messages),
                            previous_cursor=cursor,
                            new_message_ids=new_ids,
                            partial=partial,
                        )
                        try:
                            commit = session.merge_incremental(
                                merge_request, pre_current=current
                            )
                            safe_cursor = max(new_ids, key=int) if new_ids else cursor
                            grant = session.authorize_state_update(
                                commit, requested_cursor=safe_cursor
                            )
                            consumed = session.consume_state_update_grant(grant)
                        except adapter_module.RichCoreAdapterError as exc:
                            reason = (
                                "rich_incremental_merge_error"
                                if exc.category == "rich_archive_merge_failed"
                                else "rich_incremental_readback_error"
                            )
                            _queue_for_retry(queue, key, live_entry, reason=reason)
                            _persist_progress(
                                queue_path=queue_path, queue=queue,
                                state_path=state_path, state=state,
                            )
                            raise
                        _apply_consumed_grant(
                            live_entry,
                            consumed,
                            binding=audit_binding,
                            config_binding=config_binding,
                            today=args.today,
                            partial=partial,
                            mutable_plan=mutable_plan,
                        )
                        written_entries += 1
                        written_messages += len(new_ids)
                        refreshed_messages += len(messages) - len(new_ids)
                        merged_messages += len(messages)
                    else:
                        if not terminal:
                            raise DailySyncError("rich_archive_readback_failed")
                        try:
                            grant = session.authorize_quiet_update(
                                current,
                                requested_cursor=cursor,
                                explicit_terminal_empty=True,
                            )
                            consumed = session.consume_state_update_grant(grant)
                        except adapter_module.RichCoreAdapterError:
                            _queue_for_retry(
                                queue, key, live_entry,
                                reason="rich_incremental_readback_error",
                            )
                            _persist_progress(
                                queue_path=queue_path, queue=queue,
                                state_path=state_path, state=state,
                            )
                            raise
                        _apply_consumed_grant(
                            live_entry,
                            consumed,
                            binding=audit_binding,
                            config_binding=config_binding,
                            today=args.today,
                            partial=False,
                            mutable_plan=mutable_plan,
                        )
                    if partial:
                        upsert_queue_item(
                            queue,
                            key,
                            live_entry,
                            status="queued",
                            reason="rich_incremental_partial",
                        )
                        queued += 1
                    _persist_progress(
                        queue_path=queue_path, queue=queue,
                        state_path=state_path, state=state,
                    )

            active_left = sum(
                1 for item in queue.get("items", [])
                if isinstance(item, Mapping)
                and item.get("status") in ACTIVE_QUEUE_STATUSES
            )
            return {
                "ok": True,
                "status": "pending" if queued else "ok",
                "checked": checked,
                "writtenEntries": written_entries,
                "writtenMessages": written_messages,
                "refreshedMessages": refreshed_messages,
                "mergedMessages": merged_messages,
                "queued": queued,
                "totalRead": total_read,
                "activeQueueLeft": active_left,
            }, 0
    except DailySyncError:
        raise
    except Exception as exc:
        if type(exc).__module__ == adapter_module.__name__:
            raise _translate_adapter_error(exc) from exc
        raise


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
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--backup-config", required=True)
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
        status = "skipped" if exc.category == "backup_lock_busy" else "error"
        result, code = {"ok": False, "status": status, "reason": exc.category}, 2
    except Exception:
        result, code = {"ok": False, "status": "error", "reason": "unexpected_error"}, 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
