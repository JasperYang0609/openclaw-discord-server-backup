#!/usr/bin/env python3
"""Integrity-checked capability adapter for the rich Discord archive core.

Persisted JSON receipts are audit evidence only.  Runtime mutation authority is
represented by private, process-local, single-use capabilities issued by this
module while the canonical archive lock and one rich-core run context are live.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import unicodedata
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Callable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ADAPTER_VERSION = "openclaw-discord-rich-core-adapter.v3"
SLOT_PROTOCOL = "openclaw-discord-rich-locked-slot.v3"
INCREMENTAL_PROTOCOL = "openclaw-discord-rich-incremental-session.v3"
FULL_SNAPSHOT_PROTOCOL = "openclaw-discord-rich-full-snapshot-session.v2"
ROOT_CURRENT_PROTOCOL = "openclaw-discord-rich-root-current.v2"
CORE_STORAGE_CONTRACT = "openclaw-discord-rich-storage-core.v3"
RUNTIME_MANIFEST_SCHEMA = "openclaw-discord-runtime-components.v1"
INCREMENTAL_BEGIN_SCHEMA = "openclaw-discord-rich-incremental-begin.v3"
INCREMENTAL_MERGE_SCHEMA = "openclaw-discord-rich-incremental-merge.v3"
ENTRY_BINDING_SCHEMA = "openclaw-discord-daily-entry-binding.v2"
CURRENT_CAPABILITY_AUDIT_SCHEMA = "openclaw-discord-rich-current-audit.v3"
COMMIT_CAPABILITY_AUDIT_SCHEMA = "openclaw-discord-rich-commit-audit.v3"
STATE_UPDATE_GRANT_SCHEMA = "openclaw-discord-rich-state-update-grant.v3"
HEAD_PROBE_AUDIT_SCHEMA = "openclaw-discord-rich-head-probe-audit.v3"
CONFIG_BINDING_AUDIT_SCHEMA = "openclaw-discord-managed-config-binding.v1"
CANONICAL_LOCK_NAME = ".channel_backup.lock"
CAPABILITY_TTL_SECONDS = 300.0
MAX_CAPABILITY_TTL_SECONDS = 900.0
SNOWFLAKE_RE = re.compile(r"^[0-9]{1,32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ROLE_RE = re.compile(r"^daily-sync-[123]$")

ADAPTER_OPERATIONS = (
    "open_slot",
    "config_view",
    "begin_incremental",
    "inspect_current",
    "current_view",
    "probe_head",
    "head_probe_view",
    "merge_incremental",
    "authorize_state_update",
    "authorize_quiet_update",
    "consume_state_update_grant",
    "begin_full_rebuild",
)


@dataclass(frozen=True)
class AdapterContractV3:
    adapter_version: str
    slot_protocol: str
    incremental_protocol: str
    full_snapshot_protocol: str
    root_current_protocol: str
    core_storage_contract: str
    operations: tuple[str, ...]


ADAPTER_CONTRACT = AdapterContractV3(
    adapter_version=ADAPTER_VERSION,
    slot_protocol=SLOT_PROTOCOL,
    incremental_protocol=INCREMENTAL_PROTOCOL,
    full_snapshot_protocol=FULL_SNAPSHOT_PROTOCOL,
    root_current_protocol=ROOT_CURRENT_PROTOCOL,
    core_storage_contract=CORE_STORAGE_CONTRACT,
    operations=ADAPTER_OPERATIONS,
)


ERROR_CATEGORIES = frozenset({
    "backup_lock_busy",
    "rich_core_load_failed",
    "rich_core_integrity_mismatch",
    "rich_core_contract_unsupported",
    "rich_core_contract_pending",
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
})


class RichCoreAdapterError(RuntimeError):
    """Fixed, redacted failure category safe for managed receipts."""

    def __init__(self, category: str):
        if category not in ERROR_CATEGORIES:
            category = "rich_core_contract_unsupported"
        super().__init__(category)
        self.category = category


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_controls(value: str) -> None:
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise RichCoreAdapterError("rich_core_authority_invalid")


def _require_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise RichCoreAdapterError("rich_core_authority_invalid")
    _reject_controls(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RichCoreAdapterError("rich_core_authority_invalid") from exc
    if parsed.tzinfo is None:
        raise RichCoreAdapterError("rich_core_authority_invalid")
    return value


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _reject_symlink_components(path: Path) -> None:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise RichCoreAdapterError("rich_core_integrity_mismatch")


def _safe_relative_path(value: str) -> tuple[str, str]:
    _reject_controls(value)
    pure = PurePosixPath(value)
    if (
        not value
        or pure.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise RichCoreAdapterError("rich_core_authority_invalid")
    return value, unicodedata.normalize("NFKC", value).casefold()


def _safe_entry_root(archive_root: Path, relative_path: str) -> Path:
    relative, _normalized = _safe_relative_path(relative_path)
    root = _lexical_absolute(archive_root)
    result = _lexical_absolute(root.joinpath(*PurePosixPath(relative).parts))
    _reject_symlink_components(result)
    if result == root or root not in result.parents:
        raise RichCoreAdapterError("rich_core_authority_invalid")
    return result


@dataclass(frozen=True)
class EntryBindingV3:
    schema_version: str
    guild_id: str
    channel_id: str
    entry_type: str
    relative_path: str
    normalized_relative_path: str
    inventory_digest: str
    inventory_observed_at: str
    entry_binding_sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EntryBindingV3":
        expected = {
            "schemaVersion",
            "guildId",
            "channelId",
            "type",
            "relativePath",
            "normalizedRelativePath",
            "inventoryDigest",
            "inventoryObservedAt",
            "entryBindingSha256",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise RichCoreAdapterError("rich_core_authority_invalid")
        string_fields = (
            "schemaVersion",
            "guildId",
            "channelId",
            "type",
            "relativePath",
            "normalizedRelativePath",
            "inventoryDigest",
            "inventoryObservedAt",
            "entryBindingSha256",
        )
        if any(not isinstance(value.get(field), str) for field in string_fields):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        relative, normalized = _safe_relative_path(str(value.get("relativePath") or ""))
        body = {key: value[key] for key in expected if key != "entryBindingSha256"}
        if (
            value.get("schemaVersion") != ENTRY_BINDING_SCHEMA
            or not SNOWFLAKE_RE.fullmatch(str(value.get("guildId") or ""))
            or not SNOWFLAKE_RE.fullmatch(str(value.get("channelId") or ""))
            or value.get("type") not in {"channel", "thread"}
            or value.get("normalizedRelativePath") != normalized
            or not HASH_RE.fullmatch(str(value.get("inventoryDigest") or ""))
            or json_sha256(body) != value.get("entryBindingSha256")
        ):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        observed_at = _require_timestamp(value["inventoryObservedAt"])
        return cls(
            schema_version=ENTRY_BINDING_SCHEMA,
            guild_id=str(value["guildId"]),
            channel_id=str(value["channelId"]),
            entry_type=str(value["type"]),
            relative_path=relative,
            normalized_relative_path=normalized,
            inventory_digest=str(value["inventoryDigest"]),
            inventory_observed_at=observed_at,
            entry_binding_sha256=str(value["entryBindingSha256"]),
        )

    def audit_mapping(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "guildId": self.guild_id,
            "channelId": self.channel_id,
            "type": self.entry_type,
            "relativePath": self.relative_path,
            "normalizedRelativePath": self.normalized_relative_path,
            "inventoryDigest": self.inventory_digest,
            "inventoryObservedAt": self.inventory_observed_at,
            "entryBindingSha256": self.entry_binding_sha256,
        }


@dataclass(frozen=True)
class IncrementalBeginRequestV3:
    schema_version: str
    role: str
    archive_root: Path
    workspace_root: Path
    state_path: Path
    queue_path: Path
    openclaw_config_path: Path
    timezone_name: str
    inventory_digest: str
    inventory_observed_at: str
    entry_bindings: tuple[EntryBindingV3, ...]
    limits: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class IncrementalMergeRequestV3:
    schema_version: str
    entry: EntryBindingV3
    observed_at: str
    messages: tuple[Mapping[str, Any], ...]
    previous_cursor: str | None
    new_message_ids: tuple[str, ...]
    partial: bool


_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_DIR.parent
_RUNTIME_MANIFEST = _SKILL_ROOT / "manifests/runtime-components.v1.json"
_CORE_FILENAME = "rich_message_archive.py"
_ADAPTER_FILENAME = "rich_core_adapter_v3.py"
_RUNNER_FILENAME = "run_daily_sync_v3.py"
_DISPATCHER_FILENAME = "run_managed_component.py"
_COMPONENT_PATHS = {
    _CORE_FILENAME: f"scripts/{_CORE_FILENAME}",
    _ADAPTER_FILENAME: f"scripts/{_ADAPTER_FILENAME}",
    _RUNNER_FILENAME: f"scripts/{_RUNNER_FILENAME}",
    _DISPATCHER_FILENAME: f"scripts/{_DISPATCHER_FILENAME}",
}
_CORE_CACHE: tuple[tuple[Any, ...], ModuleType] | None = None
_CORE_CACHE_MUTEX = threading.RLock()
MAX_RUNTIME_COMPONENT_BYTES = 8 * 1024 * 1024
MAX_MANAGED_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _ManagedConfigBinding:
    workspace_root: Path
    config_path: Path
    config_sha256: str
    config_device: int
    config_inode: int
    config_size: int
    config_mtime_ns: int
    guild_id: str
    archive_root: Path
    state_path: Path
    queue_path: Path
    openclaw_config_path: Path
    timezone_name: str
    daily_entry_limit: int
    daily_message_limit: int
    daily_mutable_refresh_limit: int

    @property
    def authority_tuple(self) -> tuple[Any, ...]:
        return (
            self.config_sha256,
            self.config_device,
            self.config_inode,
            self.config_size,
            self.config_mtime_ns,
            json_sha256(str(self.config_path)),
            json_sha256(str(self.workspace_root)),
            json_sha256(str(self.archive_root)),
        )


def _workspace_child(workspace_root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RichCoreAdapterError("rich_core_authority_invalid")
    _reject_controls(value)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    result = _lexical_absolute(candidate)
    _reject_symlink_components(result)
    if result == workspace_root or workspace_root not in result.parents:
        raise RichCoreAdapterError("rich_core_authority_invalid")
    return result


def _absolute_managed_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RichCoreAdapterError("rich_core_authority_invalid")
    _reject_controls(value)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise RichCoreAdapterError("rich_core_authority_invalid")
    result = _lexical_absolute(candidate)
    _reject_symlink_components(result)
    if result == Path(result.anchor):
        raise RichCoreAdapterError("rich_core_authority_invalid")
    return result


def _bounded_config_int(
    value: Any, *, default: int, maximum: int
) -> int:
    candidate = default if value is None else value
    if (
        isinstance(candidate, bool)
        or not isinstance(candidate, int)
        or not 1 <= candidate <= maximum
    ):
        raise RichCoreAdapterError("rich_core_authority_invalid")
    return candidate


def _read_managed_config_bytes(path: Path) -> tuple[bytes, os.stat_result]:
    _reject_symlink_components(path)
    descriptor = -1
    try:
        before = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > MAX_MANAGED_CONFIG_BYTES
        ):
            raise RichCoreAdapterError("rich_core_integrity_mismatch")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_MANAGED_CONFIG_BYTES:
                raise RichCoreAdapterError("rich_core_integrity_mismatch")
            chunks.append(chunk)
        after = path.lstat()
        encoded = b"".join(chunks)
        if (
            len(encoded) != info.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        ):
            raise RichCoreAdapterError("rich_core_integrity_mismatch")
        return encoded, info
    except RichCoreAdapterError:
        raise
    except OSError as exc:
        raise RichCoreAdapterError("rich_core_integrity_mismatch") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_managed_config_binding(
    *,
    workspace_root: Path,
    config_path: Path,
    archive_root: Path,
) -> _ManagedConfigBinding:
    workspace = _lexical_absolute(workspace_root)
    config = _lexical_absolute(config_path)
    root = _lexical_absolute(archive_root)
    _reject_symlink_components(workspace)
    if (
        not workspace.is_dir()
        or workspace.is_symlink()
        or workspace == Path(workspace.anchor)
        or config == workspace
        or workspace not in config.parents
    ):
        raise RichCoreAdapterError("rich_core_authority_invalid")
    encoded, info = _read_managed_config_bytes(config)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RichCoreAdapterError("rich_core_authority_invalid") from exc
    if not isinstance(payload, dict):
        raise RichCoreAdapterError("rich_core_authority_invalid")
    required = {"guildId", "backupRoot", "statePath", "queuePath"}
    if not required.issubset(payload):
        raise RichCoreAdapterError("rich_core_authority_invalid")
    guild_id = payload.get("guildId")
    if not isinstance(guild_id, str) or not SNOWFLAKE_RE.fullmatch(guild_id):
        raise RichCoreAdapterError("rich_core_authority_invalid")
    configured_root = _absolute_managed_path(payload.get("backupRoot"))
    if configured_root != root:
        raise RichCoreAdapterError("rich_core_authority_invalid")
    state_path = _workspace_child(workspace, payload.get("statePath"))
    queue_path = _workspace_child(workspace, payload.get("queuePath"))
    if state_path == queue_path or config in {state_path, queue_path}:
        raise RichCoreAdapterError("rich_core_authority_invalid")
    timezone_name = payload.get("timezone", "Asia/Taipei")
    if (
        not isinstance(timezone_name, str)
        or not timezone_name
        or timezone_name != timezone_name.strip()
    ):
        raise RichCoreAdapterError("rich_core_authority_invalid")
    _reject_controls(timezone_name)
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RichCoreAdapterError("rich_core_authority_invalid") from exc
    openclaw_value = payload.get("openclawConfig")
    openclaw_path = _absolute_managed_path(
        str(Path.home() / ".openclaw/openclaw.json")
        if openclaw_value is None
        else openclaw_value
    )
    limits_value = payload.get("limits", {})
    if not isinstance(limits_value, dict):
        raise RichCoreAdapterError("rich_core_authority_invalid")
    entry_limit = _bounded_config_int(
        limits_value.get("dailyEntryLimit"), default=6, maximum=6
    )
    message_limit = _bounded_config_int(
        limits_value.get(
            "dailyMessageLimitPerEntry",
            limits_value.get("dailyMessageLimit"),
        ),
        default=60,
        maximum=60,
    )
    mutable_limit = _bounded_config_int(
        limits_value.get("dailyMutableRefreshLimit"), default=10, maximum=30
    )
    return _ManagedConfigBinding(
        workspace_root=workspace,
        config_path=config,
        config_sha256=hashlib.sha256(encoded).hexdigest(),
        config_device=int(info.st_dev),
        config_inode=int(info.st_ino),
        config_size=int(info.st_size),
        config_mtime_ns=int(info.st_mtime_ns),
        guild_id=guild_id,
        archive_root=root,
        state_path=state_path,
        queue_path=queue_path,
        openclaw_config_path=openclaw_path,
        timezone_name=timezone_name,
        daily_entry_limit=entry_limit,
        daily_message_limit=message_limit,
        daily_mutable_refresh_limit=mutable_limit,
    )


def _require_managed_config_binding(
    binding: _ManagedConfigBinding,
) -> _ManagedConfigBinding:
    current = _load_managed_config_binding(
        workspace_root=binding.workspace_root,
        config_path=binding.config_path,
        archive_root=binding.archive_root,
    )
    if current != binding:
        raise RichCoreAdapterError("rich_core_authority_invalid")
    return current


def _read_owned_component(
    path: Path, row: Mapping[str, Any]
) -> tuple[bytes, tuple[Any, ...]]:
    try:
        expected_mode = int(str(row.get("mode") or ""), 8)
    except ValueError as exc:
        raise RichCoreAdapterError("rich_core_integrity_mismatch") from exc
    _reject_symlink_components(path)
    descriptor = -1
    try:
        before = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError as exc:
        raise RichCoreAdapterError("rich_core_integrity_mismatch") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != expected_mode
        or row.get("owner") != "effective-user"
        or row.get("links") != 1
        or not HASH_RE.fullmatch(str(row.get("sha256") or ""))
    ):
        if descriptor >= 0:
            os.close(descriptor)
        raise RichCoreAdapterError("rich_core_integrity_mismatch")
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RUNTIME_COMPONENT_BYTES:
                raise RichCoreAdapterError("rich_core_integrity_mismatch")
            chunks.append(chunk)
        after = path.lstat()
    except (OSError, RichCoreAdapterError) as exc:
        if isinstance(exc, RichCoreAdapterError):
            raise
        raise RichCoreAdapterError("rich_core_integrity_mismatch") from exc
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
        raise RichCoreAdapterError("rich_core_integrity_mismatch")
    return encoded, (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, row["sha256"]
    )


def _regular_owned_component(path: Path, row: Mapping[str, Any]) -> tuple[Any, ...]:
    _encoded, identity = _read_owned_component(path, row)
    return identity


def _load_runtime_manifest() -> dict[str, Any]:
    manifest_path = _lexical_absolute(_RUNTIME_MANIFEST)
    _reject_symlink_components(manifest_path)
    descriptor = -1
    try:
        before = manifest_path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(manifest_path, flags)
        info = os.fstat(descriptor)
        encoded = os.read(descriptor, 64 * 1024 + 1)
        after = manifest_path.lstat()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RichCoreAdapterError("rich_core_integrity_mismatch") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        len(encoded) > 64 * 1024
        or len(encoded) != info.st_size
        or not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "adapterContract", "components"}
        or payload.get("schemaVersion") != RUNTIME_MANIFEST_SCHEMA
        or payload.get("adapterContract") != ADAPTER_VERSION
        or not isinstance(payload.get("components"), dict)
        or set(payload["components"]) != set(_COMPONENT_PATHS)
    ):
        raise RichCoreAdapterError("rich_core_integrity_mismatch")
    return payload


def _component_row(payload: Mapping[str, Any], filename: str) -> dict[str, Any]:
    components = payload.get("components")
    row = components.get(filename) if isinstance(components, Mapping) else None
    expected_keys = {"relativePath", "sha256", "mode", "owner", "links"}
    if (
        not isinstance(row, dict)
        or set(row) != expected_keys
        or filename not in _COMPONENT_PATHS
        or row.get("relativePath") != _COMPONENT_PATHS[filename]
    ):
        raise RichCoreAdapterError("rich_core_integrity_mismatch")
    return row


def validate_runtime_components() -> dict[str, str]:
    payload = _load_runtime_manifest()
    result: dict[str, str] = {}
    for filename in _COMPONENT_PATHS:
        row = _component_row(payload, filename)
        path = _SCRIPT_DIR / filename
        if path.parent != _SCRIPT_DIR:
            raise RichCoreAdapterError("rich_core_integrity_mismatch")
        _regular_owned_component(path, row)
        result[filename] = str(row["sha256"])
    return result


def _load_verified_core() -> ModuleType:
    global _CORE_CACHE
    payload = _load_runtime_manifest()
    adapter_row = _component_row(payload, _ADAPTER_FILENAME)
    runner_row = _component_row(payload, _RUNNER_FILENAME)
    dispatcher_row = _component_row(payload, _DISPATCHER_FILENAME)
    core_row = _component_row(payload, _CORE_FILENAME)
    _regular_owned_component(_SCRIPT_DIR / _ADAPTER_FILENAME, adapter_row)
    _regular_owned_component(_SCRIPT_DIR / _RUNNER_FILENAME, runner_row)
    _regular_owned_component(_SCRIPT_DIR / _DISPATCHER_FILENAME, dispatcher_row)
    core_source, cache_key = _read_owned_component(
        _SCRIPT_DIR / _CORE_FILENAME, core_row
    )
    with _CORE_CACHE_MUTEX:
        if _CORE_CACHE is not None and _CORE_CACHE[0] == cache_key:
            return _CORE_CACHE[1]
        name = f"_openclaw_verified_rich_core_{core_row['sha256'][:20]}"
        module = ModuleType(name)
        module.__file__ = str(_SCRIPT_DIR / _CORE_FILENAME)
        module.__package__ = ""
        sys.modules[name] = module
        try:
            compiled = compile(
                core_source,
                str(_SCRIPT_DIR / _CORE_FILENAME),
                "exec",
                dont_inherit=True,
            )
            exec(compiled, module.__dict__)
        except Exception as exc:
            sys.modules.pop(name, None)
            raise RichCoreAdapterError("rich_core_load_failed") from exc
        required = {
            "RichArchiveStore",
            "AssetDownloader",
            "AssetLimits",
            "AssetDownloadError",
            "CANONICAL_ARCHIVE_LOCK_NAME",
            "begin_incremental_run",
            "canonical_archive_lock_path",
            "normalize_message",
            "sanitize_lossless_source",
            "verify_generation",
            "load_jsonl",
            "json_sha256",
            "_load_generation_records",
            "_require_run_context",
            "_validated_lock_token_identity",
        }
        if (
            getattr(module, "RICH_CORE_CONTRACT", None) != CORE_STORAGE_CONTRACT
            or any(not hasattr(module, item) for item in required)
        ):
            raise RichCoreAdapterError("rich_core_contract_unsupported")
        _CORE_CACHE = (cache_key, module)
        return module


_PROCESS_PID = os.getpid()
_PROCESS_START_NONCE = secrets.token_hex(32)
_PROCESS_START_MONOTONIC = time.monotonic()
_CAPABILITY_GUARD = object()
_CAPABILITY_REGISTRY: dict[str, dict[str, Any]] = {}
_CAPABILITY_MUTEX = threading.RLock()


def _forget_capability(nonce: str) -> None:
    with _CAPABILITY_MUTEX:
        _CAPABILITY_REGISTRY.pop(nonce, None)


class _OpaqueCapability:
    __slots__ = ("_nonce", "_pid", "_process_nonce", "_closed", "__weakref__")

    def __init__(self, *, guard: object):
        if guard is not _CAPABILITY_GUARD:
            raise TypeError("runtime capability cannot be constructed")
        self._nonce = secrets.token_hex(32)
        self._pid = os.getpid()
        self._process_nonce = _PROCESS_START_NONCE
        self._closed = False

    def __copy__(self):
        raise TypeError("runtime capability cannot be copied")

    def __deepcopy__(self, _memo: Any):
        raise TypeError("runtime capability cannot be copied")

    def __reduce__(self):
        raise TypeError("runtime capability cannot be serialized")

    def __reduce_ex__(self, _protocol: int):
        raise TypeError("runtime capability cannot be serialized")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} opaque closed={self._closed}>"


class CurrentReadbackCapability(_OpaqueCapability):
    __slots__ = ()


class HeadProbeCapability(_OpaqueCapability):
    __slots__ = ()


class IncrementalCommitCapability(_OpaqueCapability):
    __slots__ = ()


class StateUpdateGrant(_OpaqueCapability):
    __slots__ = ()


def _mint_capability(
    capability_type: type[_OpaqueCapability],
    *,
    session: "IncrementalSessionV3",
    kind: str,
    metadata: Mapping[str, Any],
) -> _OpaqueCapability:
    capability = capability_type(guard=_CAPABILITY_GUARD)
    nonce = capability._nonce
    reference = weakref.ref(
        capability,
        lambda _reference, capability_nonce=nonce: _forget_capability(
            capability_nonce
        ),
    )
    with _CAPABILITY_MUTEX:
        _CAPABILITY_REGISTRY[nonce] = {
            "capability": reference,
            "kind": kind,
            "pid": os.getpid(),
            "processNonce": _PROCESS_START_NONCE,
            "session": weakref.ref(session),
            "sessionNonce": session._nonce,
            "leaseEpoch": session._lease_epoch,
            "configAuthority": session._config_binding.authority_tuple,
            "createdMonotonic": time.monotonic(),
            "deadlineMonotonic": time.monotonic() + session._capability_ttl_seconds,
            "consumed": False,
            "metadata": dict(metadata),
        }
        session._capability_nonces.add(nonce)
    return capability


def _invalidate_capability(nonce: str) -> None:
    with _CAPABILITY_MUTEX:
        registration = _CAPABILITY_REGISTRY.pop(nonce, None)
        capability = registration.get("capability")() if registration else None
        if capability is not None:
            capability._closed = True


def _require_capability(
    capability: Any,
    *,
    capability_type: type[_OpaqueCapability],
    kind: str,
    session: "IncrementalSessionV3",
    consume: bool = False,
) -> dict[str, Any]:
    now = time.monotonic()
    with _CAPABILITY_MUTEX:
        registration = (
            _CAPABILITY_REGISTRY.get(capability._nonce)
            if type(capability) is capability_type
            else None
        )
        if (
            type(capability) is not capability_type
            or capability._closed
            or capability._pid != os.getpid()
            or capability._process_nonce != _PROCESS_START_NONCE
            or os.getpid() != _PROCESS_PID
            or registration is None
            or registration.get("capability")() is not capability
            or registration.get("kind") != kind
            or registration.get("pid") != os.getpid()
            or registration.get("processNonce") != _PROCESS_START_NONCE
            or registration.get("session")() is not session
            or registration.get("sessionNonce") != session._nonce
            or registration.get("leaseEpoch") != session._lease_epoch
            or registration.get("configAuthority")
            != session._config_binding.authority_tuple
            or registration.get("consumed") is True
            or session._closed
        ):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        if now > float(registration.get("deadlineMonotonic") or 0):
            registration["consumed"] = True
            capability._closed = True
            raise RichCoreAdapterError("rich_core_authority_expired")
        session._require_live_authority()
        if consume:
            registration["consumed"] = True
            capability._closed = True
        return registration


def _translate_core_error(core: ModuleType, exc: BaseException, *, phase: str) -> RichCoreAdapterError:
    text = str(exc).casefold()
    if "busy" in text and phase == "lock":
        return RichCoreAdapterError("backup_lock_busy")
    if isinstance(exc, getattr(core, "AssetDownloadError")):
        return RichCoreAdapterError("rich_asset_budget_exhausted")
    if "current generation is missing" in text or "full rebuild is required" in text:
        return RichCoreAdapterError("rich_baseline_missing")
    if phase == "readback":
        return RichCoreAdapterError("rich_archive_readback_failed")
    if phase == "authority":
        return RichCoreAdapterError("rich_core_authority_invalid")
    return RichCoreAdapterError("rich_archive_merge_failed")


def _active_source_hash(core: ModuleType, record: Mapping[str, Any]) -> str:
    active_id = str(record.get("activeObservationId") or "")
    matches = [
        row for row in record.get("observations") or []
        if isinstance(row, Mapping) and row.get("observationId") == active_id
    ]
    if len(matches) != 1:
        raise RichCoreAdapterError("rich_archive_readback_failed")
    digest = str(matches[0].get("apiSourcePayloadSha256") or "")
    if not HASH_RE.fullmatch(digest):
        raise RichCoreAdapterError("rich_archive_readback_failed")
    return digest


def _all_source_hashes(record: Mapping[str, Any]) -> set[str]:
    result = {
        str(row.get("apiSourcePayloadSha256") or "")
        for row in record.get("observations") or []
        if isinstance(row, Mapping)
    }
    return {value for value in result if HASH_RE.fullmatch(value)}


class IncrementalSessionV3:
    """One exact-inventory, canonical-lock incremental capability session."""

    contract = ADAPTER_CONTRACT

    __slots__ = (
        "_core",
        "_run_context",
        "_archive_root",
        "_lock_path",
        "_lock_token",
        "_entries",
        "_inventory_digest",
        "_inventory_observed_at",
        "_config_binding",
        "_nonce",
        "_lease_epoch",
        "_capability_ttl_seconds",
        "_capability_nonces",
        "_closed",
        "__weakref__",
    )

    def __init__(
        self,
        *,
        core: ModuleType,
        run_context: Any,
        archive_root: Path,
        lock_path: Path,
        lock_token: Any,
        entries: Sequence[EntryBindingV3],
        inventory_digest: str,
        inventory_observed_at: str,
        config_binding: _ManagedConfigBinding,
        lease_epoch: str,
        capability_ttl_seconds: float,
        guard: object,
    ) -> None:
        if guard is not _CAPABILITY_GUARD:
            raise TypeError("incremental session cannot be constructed externally")
        self._core = core
        self._run_context = run_context
        self._archive_root = archive_root
        self._lock_path = lock_path
        self._lock_token = lock_token
        self._entries = {entry.entry_binding_sha256: entry for entry in entries}
        self._inventory_digest = inventory_digest
        self._inventory_observed_at = inventory_observed_at
        self._config_binding = config_binding
        self._nonce = secrets.token_hex(32)
        self._lease_epoch = lease_epoch
        self._capability_ttl_seconds = capability_ttl_seconds
        self._capability_nonces: set[str] = set()
        self._closed = False

    def __enter__(self) -> "IncrementalSessionV3":
        self._require_live_authority()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for nonce in list(self._capability_nonces):
            _invalidate_capability(nonce)
        self._capability_nonces.clear()
        self._run_context.close()

    def _require_live_authority(self) -> dict[str, Any]:
        if self._closed or os.getpid() != _PROCESS_PID:
            raise RichCoreAdapterError("rich_core_authority_invalid")
        _require_managed_config_binding(self._config_binding)
        try:
            registration = self._core._require_run_context(
                self._run_context,
                kind="incremental",
                lock_token=self._lock_token,
            )
            lock_identity = self._core._validated_lock_token_identity(
                self._lock_token, allow_closing_owner=True
            )
        except Exception as exc:
            raise _translate_core_error(self._core, exc, phase="authority") from exc
        if (
            Path(registration["archiveRoot"]) != self._archive_root
            or Path(registration["lockPath"]) != self._lock_path
            or Path(lock_identity["path"]) != self._lock_path
            or registration["inventoryDigest"] != json_sha256(sorted([
                {
                    "channelId": entry.channel_id,
                    "relativePath": entry.relative_path,
                    "normalizedRelativePath": entry.normalized_relative_path,
                }
                for entry in self._entries.values()
            ], key=lambda row: (int(row["channelId"]), row["normalizedRelativePath"])))
        ):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        return registration

    def _entry(self, entry: EntryBindingV3) -> EntryBindingV3:
        if type(entry) is not EntryBindingV3:
            raise RichCoreAdapterError("rich_core_authority_invalid")
        registered = self._entries.get(entry.entry_binding_sha256)
        if registered is not entry and registered != entry:
            raise RichCoreAdapterError("rich_core_authority_invalid")
        if (
            entry.inventory_digest != self._inventory_digest
            or entry.inventory_observed_at != self._inventory_observed_at
        ):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        return entry

    def _store(self, entry: EntryBindingV3):
        root = _safe_entry_root(self._archive_root, entry.relative_path)
        return self._core.RichArchiveStore(root, lock_path=self._lock_path)

    def _inspect_metadata(self, entry: EntryBindingV3) -> dict[str, Any]:
        entry = self._entry(entry)
        store = self._store(entry)
        try:
            current = store.resolve_current()
            if current is None:
                raise RichCoreAdapterError("rich_baseline_missing")
            pointer_before = store.pointer_path.read_bytes()
            verification = self._core.verify_generation(current)
            records, _by_day, duplicates = self._core._load_generation_records(current)
            pointer_after = store.pointer_path.read_bytes()
        except RichCoreAdapterError:
            raise
        except Exception as exc:
            raise _translate_core_error(self._core, exc, phase="readback") from exc
        if (
            pointer_before != pointer_after
            or not verification.get("ok")
            or duplicates != 0
        ):
            raise RichCoreAdapterError("rich_archive_readback_failed")
        ids = sorted((str(value) for value in records), key=int)
        if any(
            not SNOWFLAKE_RE.fullmatch(message_id)
            or str(records[message_id].get("channelId") or "") != entry.channel_id
            for message_id in ids
        ):
            raise RichCoreAdapterError("rich_archive_readback_failed")
        active = {message_id: _active_source_hash(self._core, records[message_id]) for message_id in ids}
        audited_empty = bool(
            not ids
            and verification.get("gateStatus") == "AUDIT_ONLY"
            and verification.get("fullGatePresent") is True
            and not verification.get("fullGateErrors")
        )
        registration = self._require_live_authority()
        lock_identity = self._core._validated_lock_token_identity(
            self._lock_token, allow_closing_owner=True
        )
        return {
            "entry": entry,
            "entryRoot": _safe_entry_root(self._archive_root, entry.relative_path),
            "entryRootSha256": json_sha256(str(_safe_entry_root(self._archive_root, entry.relative_path))),
            "generationId": current.name,
            "generationSha256": str(verification["generationSha256"]),
            "pointerSha256": hashlib.sha256(pointer_before).hexdigest(),
            "canonicalMessageIds": tuple(ids),
            "activeApiSourcePayloadSha256ById": active,
            "auditedEmptyBaseline": audited_empty,
            "runContextId": str(registration["runContextId"]),
            "budgetObjectIdentity": id(registration["budget"]),
            "lockDevice": int(lock_identity["device"]),
            "lockInode": int(lock_identity["inode"]),
            "lockPathSha256": json_sha256(str(self._lock_path)),
            "configAuthority": self._config_binding.authority_tuple,
        }

    @staticmethod
    def _same_current(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
        keys = (
            "entryRootSha256",
            "generationId",
            "generationSha256",
            "pointerSha256",
            "canonicalMessageIds",
            "activeApiSourcePayloadSha256ById",
            "runContextId",
            "budgetObjectIdentity",
            "lockDevice",
            "lockInode",
            "lockPathSha256",
            "configAuthority",
        )
        return all(first.get(key) == second.get(key) for key in keys)

    def inspect_current(self, entry: EntryBindingV3) -> CurrentReadbackCapability:
        metadata = self._inspect_metadata(entry)
        return _mint_capability(
            CurrentReadbackCapability,
            session=self,
            kind="current",
            metadata=metadata,
        )  # type: ignore[return-value]

    def current_view(self, current: CurrentReadbackCapability) -> dict[str, Any]:
        registration = _require_capability(
            current,
            capability_type=CurrentReadbackCapability,
            kind="current",
            session=self,
        )
        row = registration["metadata"]
        return {
            "schemaVersion": CURRENT_CAPABILITY_AUDIT_SCHEMA,
            "authority": "AUDIT_ONLY",
            "entryBindingSha256": row["entry"].entry_binding_sha256,
            "channelId": row["entry"].channel_id,
            "generationId": row["generationId"],
            "generationSha256": row["generationSha256"],
            "pointerSha256": row["pointerSha256"],
            "canonicalMessageIds": list(row["canonicalMessageIds"]),
            "activeApiSourcePayloadSha256ById": dict(row["activeApiSourcePayloadSha256ById"]),
            "auditedEmptyBaseline": row["auditedEmptyBaseline"],
        }

    def probe_head(
        self,
        entry: EntryBindingV3,
        *,
        fetch_page: Callable[[str, int], Sequence[Mapping[str, Any]]],
        limit: int = 30,
    ) -> HeadProbeCapability:
        entry = self._entry(entry)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise RichCoreAdapterError("rich_core_authority_invalid")
        baseline = self._inspect_metadata(entry)
        if not baseline["auditedEmptyBaseline"] or baseline["canonicalMessageIds"]:
            raise RichCoreAdapterError("rich_baseline_missing")
        try:
            page_value = fetch_page(entry.channel_id, limit)
        except RichCoreAdapterError:
            raise
        except Exception as exc:
            raise RichCoreAdapterError("rich_archive_readback_failed") from exc
        if not isinstance(page_value, Sequence) or isinstance(page_value, (str, bytes, bytearray)):
            raise RichCoreAdapterError("rich_archive_readback_failed")
        rows = [dict(row) for row in page_value if isinstance(row, Mapping)]
        if len(rows) != len(page_value) or len(rows) > limit:
            raise RichCoreAdapterError("rich_archive_readback_failed")
        ids = [str(row.get("id") or "") for row in rows]
        if (
            any(not SNOWFLAKE_RE.fullmatch(value) for value in ids)
            or len(ids) != len(set(ids))
            or any(str(row.get("channel_id") or entry.channel_id) != entry.channel_id for row in rows)
        ):
            raise RichCoreAdapterError("rich_archive_readback_failed")
        after = self._inspect_metadata(entry)
        if not self._same_current(baseline, after):
            raise RichCoreAdapterError("rich_archive_readback_failed")
        metadata = {
            "entry": entry,
            "baseline": baseline,
            "messageIds": tuple(sorted(ids, key=int)),
            "rawPayloadSha256ById": {
                str(row["id"]): self._core.json_sha256(self._core.sanitize_lossless_source(row))
                for row in rows
            },
            "explicitEmpty": not rows,
            "limit": limit,
        }
        return _mint_capability(
            HeadProbeCapability,
            session=self,
            kind="head",
            metadata=metadata,
        )  # type: ignore[return-value]

    def head_probe_view(self, probe: HeadProbeCapability) -> dict[str, Any]:
        registration = _require_capability(
            probe,
            capability_type=HeadProbeCapability,
            kind="head",
            session=self,
        )
        row = registration["metadata"]
        return {
            "schemaVersion": HEAD_PROBE_AUDIT_SCHEMA,
            "authority": "AUDIT_ONLY",
            "entryBindingSha256": row["entry"].entry_binding_sha256,
            "channelId": row["entry"].channel_id,
            "messageIds": list(row["messageIds"]),
            "explicitEmpty": row["explicitEmpty"],
        }

    def merge_incremental(
        self,
        request: IncrementalMergeRequestV3,
        *,
        pre_current: CurrentReadbackCapability,
    ) -> IncrementalCommitCapability:
        if type(request) is not IncrementalMergeRequestV3 or request.schema_version != INCREMENTAL_MERGE_SCHEMA:
            raise RichCoreAdapterError("rich_core_authority_invalid")
        entry = self._entry(request.entry)
        if (
            not isinstance(request.observed_at, str)
            or type(request.partial) is not bool
            or not request.messages
        ):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        _require_timestamp(request.observed_at)
        current_registration = _require_capability(
            pre_current,
            capability_type=CurrentReadbackCapability,
            kind="current",
            session=self,
        )
        pre = current_registration["metadata"]
        if pre["entry"] != entry:
            raise RichCoreAdapterError("rich_core_authority_invalid")
        refreshed = self._inspect_metadata(entry)
        if not self._same_current(pre, refreshed):
            raise RichCoreAdapterError("rich_archive_readback_failed")
        messages = tuple(dict(row) for row in request.messages if isinstance(row, Mapping))
        if len(messages) != len(request.messages):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        message_ids = [str(row.get("id") or "") for row in messages]
        new_ids = [str(value) for value in request.new_message_ids]
        if (
            any(not SNOWFLAKE_RE.fullmatch(value) for value in message_ids + new_ids)
            or len(message_ids) != len(set(message_ids))
            or new_ids != sorted(set(new_ids), key=int)
            or not set(new_ids).issubset(message_ids)
            or (
                request.previous_cursor is not None
                and (
                    not SNOWFLAKE_RE.fullmatch(request.previous_cursor)
                    or request.previous_cursor not in pre["canonicalMessageIds"]
                    or any(int(value) <= int(request.previous_cursor) for value in new_ids)
                )
            )
            or (request.previous_cursor is None and new_ids)
        ):
            raise RichCoreAdapterError("rich_cursor_not_authorized")
        _require_capability(
            pre_current,
            capability_type=CurrentReadbackCapability,
            kind="current",
            session=self,
            consume=True,
        )
        raw_hashes = {
            message_id: self._core.json_sha256(
                self._core.sanitize_lossless_source(message)
            )
            for message_id, message in zip(message_ids, messages)
        }
        normalized_hashes: dict[str, str] = {}
        try:
            for message_id, message in zip(message_ids, messages):
                normalized = self._core.normalize_message(
                    message,
                    expected_channel_id=entry.channel_id,
                    observed_at=request.observed_at,
                )
                normalized_hashes[message_id] = _active_source_hash(self._core, normalized)
            generation_id = (
                datetime.now(timezone.utc).strftime("daily-%Y%m%dT%H%M%S%fZ-")
                + secrets.token_hex(12)
            )
            store = self._store(entry)
            result = store.merge_messages(
                messages,
                channel_id=entry.channel_id,
                relative_path=entry.relative_path,
                observed_at=request.observed_at,
                generation_id=generation_id,
                downloader=self._core.AssetDownloader(),
                lock_token=self._lock_token,
                run_context=self._run_context,
            )
        except Exception as exc:
            raise _translate_core_error(self._core, exc, phase="merge") from exc
        post = self._inspect_metadata(entry)
        if (
            result.get("generationId") != post["generationId"]
            or result.get("generationSha256") != post["generationSha256"]
            or not set(message_ids).issubset(post["canonicalMessageIds"])
        ):
            raise RichCoreAdapterError("rich_archive_readback_failed")
        records, _days, duplicates = self._core._load_generation_records(
            self._store(entry).resolve_current()
        )
        if duplicates:
            raise RichCoreAdapterError("rich_archive_readback_failed")
        for message_id in message_ids:
            if normalized_hashes[message_id] not in _all_source_hashes(records[message_id]):
                raise RichCoreAdapterError("rich_archive_readback_failed")
        registration = self._require_live_authority()
        budget = registration["budget"]
        budget_receipt = {
            "runContextId": registration["runContextId"],
            "leaseEpoch": self._lease_epoch,
            "budgetObjectIdentity": id(budget),
            "fileCount": budget.file_count,
            "declaredBytes": budget.declared_bytes,
            "limitsSha256": json_sha256(vars(budget.limits)),
        }
        lock_receipt = {
            "runContextId": registration["runContextId"],
            "leaseEpoch": self._lease_epoch,
            "lockPathSha256": post["lockPathSha256"],
            "lockDevice": post["lockDevice"],
            "lockInode": post["lockInode"],
        }
        safe_cursor = (
            max(new_ids, key=int) if new_ids else request.previous_cursor
        )
        metadata = {
            "entry": entry,
            "pre": pre,
            "post": post,
            "messageIds": tuple(sorted(message_ids, key=int)),
            "newMessageIds": tuple(new_ids),
            "rawPayloadSha256ById": raw_hashes,
            "normalizedSourceSha256ById": normalized_hashes,
            "safeCursor": safe_cursor,
            "partial": request.partial,
            "observedAt": request.observed_at,
            "lockReceiptSha256": json_sha256(lock_receipt),
            "budgetReceiptSha256": json_sha256(budget_receipt),
        }
        return _mint_capability(
            IncrementalCommitCapability,
            session=self,
            kind="commit",
            metadata=metadata,
        )  # type: ignore[return-value]

    def commit_view(self, commit: IncrementalCommitCapability) -> dict[str, Any]:
        registration = _require_capability(
            commit,
            capability_type=IncrementalCommitCapability,
            kind="commit",
            session=self,
        )
        row = registration["metadata"]
        return {
            "schemaVersion": COMMIT_CAPABILITY_AUDIT_SCHEMA,
            "authority": "AUDIT_ONLY",
            "entryBindingSha256": row["entry"].entry_binding_sha256,
            "channelId": row["entry"].channel_id,
            "preGenerationId": row["pre"]["generationId"],
            "committedGenerationId": row["post"]["generationId"],
            "committedGenerationSha256": row["post"]["generationSha256"],
            "currentPointerSha256": row["post"]["pointerSha256"],
            "fetchedMessageIds": list(row["messageIds"]),
            "newMessageIds": list(row["newMessageIds"]),
            "safeCursor": row["safeCursor"],
            "partial": row["partial"],
            "lockReceiptSha256": row["lockReceiptSha256"],
            "budgetReceiptSha256": row["budgetReceiptSha256"],
        }

    def authorize_state_update(
        self,
        commit: IncrementalCommitCapability,
        *,
        requested_cursor: str | None,
    ) -> StateUpdateGrant:
        registration = _require_capability(
            commit,
            capability_type=IncrementalCommitCapability,
            kind="commit",
            session=self,
        )
        row = registration["metadata"]
        if requested_cursor != row["safeCursor"]:
            raise RichCoreAdapterError("rich_cursor_not_authorized")
        if requested_cursor is not None and (
            not SNOWFLAKE_RE.fullmatch(requested_cursor)
            or requested_cursor not in row["post"]["canonicalMessageIds"]
        ):
            raise RichCoreAdapterError("rich_cursor_not_authorized")
        current = self._inspect_metadata(row["entry"])
        if not self._same_current(row["post"], current):
            raise RichCoreAdapterError("rich_archive_readback_failed")
        _require_capability(
            commit,
            capability_type=IncrementalCommitCapability,
            kind="commit",
            session=self,
            consume=True,
        )
        metadata = {
            "entry": row["entry"],
            "post": current,
            "authorizedCursor": requested_cursor,
            "committedMessageIds": row["messageIds"],
            "newMessageIds": row["newMessageIds"],
            "partial": row["partial"],
            "observedAt": row["observedAt"],
            "quiet": False,
        }
        return _mint_capability(
            StateUpdateGrant,
            session=self,
            kind="grant",
            metadata=metadata,
        )  # type: ignore[return-value]

    def authorize_quiet_update(
        self,
        current: CurrentReadbackCapability,
        *,
        requested_cursor: str | None,
        head_probe: HeadProbeCapability | None = None,
        explicit_terminal_empty: bool = False,
    ) -> StateUpdateGrant:
        registration = _require_capability(
            current,
            capability_type=CurrentReadbackCapability,
            kind="current",
            session=self,
        )
        row = registration["metadata"]
        current_now = self._inspect_metadata(row["entry"])
        if not self._same_current(row, current_now):
            raise RichCoreAdapterError("rich_archive_readback_failed")
        if requested_cursor is None:
            if head_probe is None:
                raise RichCoreAdapterError("rich_baseline_missing")
            probe_registration = _require_capability(
                head_probe,
                capability_type=HeadProbeCapability,
                kind="head",
                session=self,
            )
            probe = probe_registration["metadata"]
            if (
                probe["entry"] != row["entry"]
                or not probe["explicitEmpty"]
                or not row["auditedEmptyBaseline"]
                or not self._same_current(row, probe["baseline"])
            ):
                raise RichCoreAdapterError("rich_baseline_missing")
            _require_capability(
                head_probe,
                capability_type=HeadProbeCapability,
                kind="head",
                session=self,
                consume=True,
            )
        elif (
            not explicit_terminal_empty
            or not SNOWFLAKE_RE.fullmatch(requested_cursor)
            or requested_cursor not in row["canonicalMessageIds"]
            or head_probe is not None
        ):
            raise RichCoreAdapterError("rich_cursor_not_authorized")
        _require_capability(
            current,
            capability_type=CurrentReadbackCapability,
            kind="current",
            session=self,
            consume=True,
        )
        metadata = {
            "entry": row["entry"],
            "post": current_now,
            "authorizedCursor": requested_cursor,
            "committedMessageIds": (),
            "newMessageIds": (),
            "partial": False,
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "quiet": True,
        }
        return _mint_capability(
            StateUpdateGrant,
            session=self,
            kind="grant",
            metadata=metadata,
        )  # type: ignore[return-value]

    def consume_state_update_grant(self, grant: StateUpdateGrant) -> dict[str, Any]:
        registration = _require_capability(
            grant,
            capability_type=StateUpdateGrant,
            kind="grant",
            session=self,
        )
        row = registration["metadata"]
        current = self._inspect_metadata(row["entry"])
        if not self._same_current(row["post"], current):
            raise RichCoreAdapterError("rich_archive_readback_failed")
        _require_capability(
            grant,
            capability_type=StateUpdateGrant,
            kind="grant",
            session=self,
            consume=True,
        )
        return {
            "schemaVersion": STATE_UPDATE_GRANT_SCHEMA,
            "authority": "CONSUMED_RUNTIME_GRANT",
            "entryBindingSha256": row["entry"].entry_binding_sha256,
            "channelId": row["entry"].channel_id,
            "generationId": current["generationId"],
            "generationSha256": current["generationSha256"],
            "pointerSha256": current["pointerSha256"],
            "authorizedCursor": row["authorizedCursor"],
            "committedMessageIds": list(row["committedMessageIds"]),
            "newMessageIds": list(row["newMessageIds"]),
            "partial": row["partial"],
            "quiet": row["quiet"],
            "observedAt": row["observedAt"],
            "inventoryDigest": self._inventory_digest,
            "inventoryObservedAt": self._inventory_observed_at,
            "configSha256": self._config_binding.config_sha256,
            "configPathSha256": json_sha256(str(self._config_binding.config_path)),
        }


class LockedRichSlotV3:
    """One-shot canonical-lock slot; mutable JSON may load only after entry."""

    contract = ADAPTER_CONTRACT

    __slots__ = (
        "_core",
        "_archive_root",
        "_lock_path",
        "_lock_token",
        "_lease_epoch",
        "_capability_ttl_seconds",
        "_config_binding",
        "_session_started",
        "_closed",
    )

    def __init__(
        self,
        *,
        core: ModuleType,
        archive_root: Path,
        lock_path: Path,
        lock_token: Any,
        capability_ttl_seconds: float,
        config_binding: _ManagedConfigBinding,
        guard: object,
    ) -> None:
        if guard is not _CAPABILITY_GUARD:
            raise TypeError("locked slot cannot be constructed externally")
        self._core = core
        self._archive_root = archive_root
        self._lock_path = lock_path
        self._lock_token = lock_token
        self._lease_epoch = secrets.token_hex(32)
        self._capability_ttl_seconds = capability_ttl_seconds
        self._config_binding = config_binding
        self._session_started = False
        self._closed = False

    def __enter__(self) -> "LockedRichSlotV3":
        if self._closed:
            raise RichCoreAdapterError("rich_core_authority_invalid")
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._lock_token.close()

    def config_view(self) -> dict[str, Any]:
        binding = _require_managed_config_binding(self._config_binding)
        return {
            "schemaVersion": CONFIG_BINDING_AUDIT_SCHEMA,
            "authority": "AUDIT_ONLY",
            "configSha256": binding.config_sha256,
            "configPathSha256": json_sha256(str(binding.config_path)),
            "workspaceRootSha256": json_sha256(str(binding.workspace_root)),
            "archiveRootSha256": json_sha256(str(binding.archive_root)),
            "guildId": binding.guild_id,
            "timezone": binding.timezone_name,
            "statePathSha256": json_sha256(str(binding.state_path)),
            "queuePathSha256": json_sha256(str(binding.queue_path)),
            "openclawConfigPathSha256": json_sha256(
                str(binding.openclaw_config_path)
            ),
            "dailyEntryLimit": binding.daily_entry_limit,
            "dailyMessageLimit": binding.daily_message_limit,
            "dailyMutableRefreshLimit": binding.daily_mutable_refresh_limit,
        }

    @contextmanager
    def begin_incremental(
        self, request: IncrementalBeginRequestV3
    ) -> Iterator[IncrementalSessionV3]:
        if self._closed or self._session_started:
            raise RichCoreAdapterError("rich_core_authority_invalid")
        if type(request) is not IncrementalBeginRequestV3:
            raise RichCoreAdapterError("rich_core_contract_unsupported")
        if (
            request.schema_version != INCREMENTAL_BEGIN_SCHEMA
            or not ROLE_RE.fullmatch(request.role)
            or not isinstance(request.archive_root, Path)
            or _lexical_absolute(request.archive_root) != self._archive_root
            or not isinstance(request.workspace_root, Path)
            or _lexical_absolute(request.workspace_root)
            != self._config_binding.workspace_root
            or not isinstance(request.state_path, Path)
            or _lexical_absolute(request.state_path) != self._config_binding.state_path
            or not isinstance(request.queue_path, Path)
            or _lexical_absolute(request.queue_path) != self._config_binding.queue_path
            or not isinstance(request.openclaw_config_path, Path)
            or _lexical_absolute(request.openclaw_config_path)
            != self._config_binding.openclaw_config_path
            or request.timezone_name != self._config_binding.timezone_name
            or not HASH_RE.fullmatch(request.inventory_digest)
            or not request.entry_bindings
            or any(type(entry) is not EntryBindingV3 for entry in request.entry_bindings)
            or any(entry.inventory_digest != request.inventory_digest for entry in request.entry_bindings)
            or any(entry.inventory_observed_at != request.inventory_observed_at for entry in request.entry_bindings)
            or len({entry.channel_id for entry in request.entry_bindings}) != len(request.entry_bindings)
            or len({entry.normalized_relative_path for entry in request.entry_bindings}) != len(request.entry_bindings)
            or len({entry.guild_id for entry in request.entry_bindings}) != 1
            or request.entry_bindings[0].guild_id != self._config_binding.guild_id
        ):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        _require_timestamp(request.inventory_observed_at)
        for entry in request.entry_bindings:
            _safe_entry_root(self._archive_root, entry.relative_path)
        limits = dict(request.limits)
        expected_limits = {
            "maxEntries",
            "maxWriteEntries",
            "maxReadMessages",
            "maxMessagesPerEntry",
            "mutableRefreshLimit",
        }
        if (
            set(limits) != expected_limits
            or len(request.limits) != len(expected_limits)
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in limits.values())
        ):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        if (
            limits["maxEntries"] != self._config_binding.daily_entry_limit
            or limits["maxWriteEntries"] != 4
            or limits["maxReadMessages"] != 180
            or limits["maxMessagesPerEntry"]
            != self._config_binding.daily_message_limit
            or limits["mutableRefreshLimit"]
            != self._config_binding.daily_mutable_refresh_limit
        ):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        entries = [
            {
                "channelId": entry.channel_id,
                "relativePath": entry.relative_path,
                "normalizedRelativePath": entry.normalized_relative_path,
            }
            for entry in request.entry_bindings
        ]
        try:
            run_context = self._core.begin_incremental_run(
                entries=entries,
                archive_root=self._archive_root,
                lock_token=self._lock_token,
                limits=self._core.AssetLimits(),
            )
        except Exception as exc:
            raise _translate_core_error(self._core, exc, phase="authority") from exc
        self._session_started = True
        session = IncrementalSessionV3(
            core=self._core,
            run_context=run_context,
            archive_root=self._archive_root,
            lock_path=self._lock_path,
            lock_token=self._lock_token,
            entries=request.entry_bindings,
            inventory_digest=request.inventory_digest,
            inventory_observed_at=request.inventory_observed_at,
            config_binding=self._config_binding,
            lease_epoch=self._lease_epoch,
            capability_ttl_seconds=self._capability_ttl_seconds,
            guard=_CAPABILITY_GUARD,
        )
        try:
            yield session
        finally:
            session.close()

    def begin_full_rebuild(self, _request: Any):
        raise RichCoreAdapterError("rich_core_contract_pending")


class ManagedRichCoreAdapterV3:
    """Production adapter with no caller-supplied core or factory hooks."""

    contract = ADAPTER_CONTRACT

    __slots__ = ("_core", "_capability_ttl_seconds")

    def __init__(self, *, capability_ttl_seconds: float = CAPABILITY_TTL_SECONDS):
        if (
            not isinstance(capability_ttl_seconds, (int, float))
            or isinstance(capability_ttl_seconds, bool)
            or not 0 < float(capability_ttl_seconds) <= MAX_CAPABILITY_TTL_SECONDS
        ):
            raise RichCoreAdapterError("rich_core_contract_unsupported")
        self._core = _load_verified_core()
        self._capability_ttl_seconds = float(capability_ttl_seconds)

    @contextmanager
    def open_slot(
        self,
        *,
        archive_root: Path,
        lock_path: Path | None = None,
        workspace_root: Path,
        config_path: Path,
    ) -> Iterator[LockedRichSlotV3]:
        root = _lexical_absolute(Path(archive_root))
        _reject_symlink_components(root)
        if not root.is_dir() or root.is_symlink() or root == Path(root.anchor):
            raise RichCoreAdapterError("rich_core_authority_invalid")
        canonical_lock = self._core.canonical_archive_lock_path(root)
        if (
            canonical_lock != root / CANONICAL_LOCK_NAME
            or self._core.CANONICAL_ARCHIVE_LOCK_NAME != CANONICAL_LOCK_NAME
        ):
            raise RichCoreAdapterError("rich_core_contract_unsupported")
        supplied = canonical_lock if lock_path is None else _lexical_absolute(Path(lock_path))
        if supplied != canonical_lock:
            raise RichCoreAdapterError("rich_core_authority_invalid")
        store = self._core.RichArchiveStore(
            root / ".rich-adapter-lock-owner",
            lock_path=canonical_lock,
        )
        try:
            token = store.acquire_lock()
        except Exception as exc:
            raise _translate_core_error(self._core, exc, phase="lock") from exc
        try:
            config_binding = _load_managed_config_binding(
                workspace_root=Path(workspace_root),
                config_path=Path(config_path),
                archive_root=root,
            )
        except BaseException:
            token.close()
            raise
        slot = LockedRichSlotV3(
            core=self._core,
            archive_root=root,
            lock_path=canonical_lock,
            lock_token=token,
            capability_ttl_seconds=self._capability_ttl_seconds,
            config_binding=config_binding,
            guard=_CAPABILITY_GUARD,
        )
        try:
            yield slot
        finally:
            slot.close()


__all__ = [
    "ADAPTER_CONTRACT",
    "ADAPTER_VERSION",
    "AdapterContractV3",
    "CANONICAL_LOCK_NAME",
    "COMMIT_CAPABILITY_AUDIT_SCHEMA",
    "CONFIG_BINDING_AUDIT_SCHEMA",
    "CORE_STORAGE_CONTRACT",
    "CURRENT_CAPABILITY_AUDIT_SCHEMA",
    "CurrentReadbackCapability",
    "EntryBindingV3",
    "FULL_SNAPSHOT_PROTOCOL",
    "HEAD_PROBE_AUDIT_SCHEMA",
    "HeadProbeCapability",
    "INCREMENTAL_BEGIN_SCHEMA",
    "INCREMENTAL_MERGE_SCHEMA",
    "INCREMENTAL_PROTOCOL",
    "IncrementalBeginRequestV3",
    "IncrementalCommitCapability",
    "IncrementalMergeRequestV3",
    "IncrementalSessionV3",
    "LockedRichSlotV3",
    "ManagedRichCoreAdapterV3",
    "ROOT_CURRENT_PROTOCOL",
    "RichCoreAdapterError",
    "SLOT_PROTOCOL",
    "STATE_UPDATE_GRANT_SCHEMA",
    "StateUpdateGrant",
    "canonical_json",
    "file_sha256",
    "json_sha256",
    "validate_runtime_components",
]
