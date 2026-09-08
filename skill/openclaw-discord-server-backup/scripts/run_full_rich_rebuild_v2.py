#!/usr/bin/env python3
"""Fail-closed coordinator for one full rich Discord archive rebuild.

This module owns orchestration and durable, audit-only coordination records.  It
does not implement Discord transport, rich normalization, asset downloads, or
pointer mutation.  Those operations belong to the reviewed Adapter V3 core and
are represented here only by nominal, opaque capability ports.

Persisted JSON is never accepted as runtime authority.  Production loads one
fixed sibling adapter only when an integrity manifest binds its exact bytes and
contract.  Tests may inject a nominal adapter subclass to exercise the saga
without changing the live archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import types
import unicodedata
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence


COORDINATOR_SCHEMA = "openclaw-discord-full-rich-rebuild-coordinator.v2"
JOURNAL_SCHEMA = "openclaw-discord-full-rich-rebuild-journal.v2"
JOURNAL_ENVELOPE_SCHEMA = "openclaw-discord-full-rich-rebuild-journal-envelope.v1"
EVENT_RECEIPT_SCHEMA = "openclaw-discord-full-rich-rebuild-event.v1"
FINAL_RECEIPT_SCHEMA = "openclaw-discord-full-rich-rebuild-run.v1"
APPROVED_BASELINE_SCHEMA = "openclaw-discord-full-rich-approved-baseline.v2"
RUNTIME_MANIFEST_SCHEMA = "openclaw-discord-full-rich-runtime-components.v1"
RUNTIME_MANIFEST_RELATIVE_PATH = (
    "manifests/full-rich-runtime-components.v1.json"
)
COMMITTED_READBACK_AUDIT_SCHEMA = (
    "openclaw-discord-rich-committed-readback-audit.v2"
)
RICH_CORE_ADAPTER_VERSION = "openclaw-discord-rich-core-adapter.v3"
RICH_SLOT_PROTOCOL = "openclaw-discord-rich-locked-slot.v3"
RICH_FULL_SESSION_PROTOCOL = "openclaw-discord-rich-full-snapshot-session.v2"
RICH_ROOT_CURRENT_PROTOCOL = "openclaw-discord-rich-root-current.v2"

INVENTORY_AUDIT_SCHEMA = "openclaw-discord-rich-inventory-audit.v3"
PERMISSION_AUDIT_SCHEMA = "openclaw-discord-rich-permission-audit.v2"
BASELINE_AUDIT_SCHEMA = "openclaw-discord-rich-baseline-audit.v2"
RESOURCE_AUDIT_SCHEMA = "openclaw-discord-rich-resource-reservation-audit.v2"
SEALED_ENTRY_AUDIT_SCHEMA = "openclaw-discord-rich-sealed-entry-audit.v2"
ROUND_AUDIT_SCHEMA = "openclaw-discord-rich-round-audit.v2"
RUN_READY_AUDIT_SCHEMA = "openclaw-discord-rich-run-ready-audit.v2"
ROOT_CURRENT_AUDIT_SCHEMA = "openclaw-discord-rich-root-current-audit.v2"
ROOT_GRANT_AUDIT_SCHEMA = "openclaw-discord-rich-root-grant-audit.v2"
COMPATIBILITY_AUDIT_SCHEMA = "openclaw-discord-rich-compatibility-audit.v2"

RUNTIME_COMPONENT_PATHS = {
    "adapter": "scripts/rich_core_adapter_v3.py",
    "richCore": "scripts/rich_message_archive.py",
    "coordinator": "scripts/run_full_rich_rebuild_v2.py",
    "dailyRunner": "scripts/run_daily_sync_v3.py",
    "managedComponent": "scripts/run_managed_component.py",
    "canonicalConfig": "manifests/full-rich-rebuild-config.v1.json",
    "expectedEntries": "manifests/full-rich-rebuild-entries.v1.json",
}
_PRODUCTION_AUTHORITY = object()

REQUIRED_INVENTORY_CLASSES = (
    "guild_channels",
    "active_threads",
    "archived_public_threads",
    "archived_private_threads",
    "joined_archived_private_threads",
)
ROUND_KINDS = ("baseline", "delta", "zero")
PHASES = (
    "PREPARED",
    "INVENTORY_VERIFIED",
    "BASELINE_FROZEN",
    "REBUILDING",
    "BASELINE_COMPLETE",
    "DELTA_CONVERGING",
    "VERIFYING",
    "READY_TO_COMMIT",
    "COMMITTED",
)
TERMINAL_PHASES = frozenset({"PAUSED", "FAILED"})
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SNOWFLAKE_RE = re.compile(r"^[0-9]{1,32}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,127}$")
BIDI_CONTROL_CODEPOINTS = frozenset(
    {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}
)

ERROR_REASONS = frozenset({
    "unsafe_input_path",
    "invalid_run_id",
    "invalid_expected_entries",
    "expected_entry_count_mismatch",
    "expected_entry_set_digest_mismatch",
    "state_hash_mismatch",
    "queue_hash_mismatch",
    "state_queue_drift",
    "baseline_verification_failed",
    "inventory_incomplete",
    "inventory_count_mismatch",
    "inventory_identity_mismatch",
    "inventory_drift",
    "permission_evidence_failed",
    "message_content_unavailable",
    "rich_core_contract_pending",
    "rich_core_contract_unsupported",
    "rich_core_integrity_mismatch",
    "rich_core_authority_invalid",
    "rich_core_authority_replayed",
    "rich_core_authority_expired",
    "rich_full_stage_failed",
    "rich_full_evidence_failed",
    "rich_full_run_incomplete",
    "rich_asset_budget_exhausted",
    "rich_disk_reservation_failed",
    "rich_convergence_exhausted",
    "rich_pagination_incomplete",
    "rich_entry_current_mutated_early",
    "rich_coverage_incomplete",
    "rich_root_publish_failed",
    "rich_root_readback_failed",
    "rich_root_rollback_failed",
    "rich_compatibility_publish_failed",
    "committed_readback_failed",
    "journal_corrupt",
    "journal_binding_mismatch",
    "receipt_chain_corrupt",
    "unexpected_error",
})


class FullRebuildError(RuntimeError):
    """Bounded operator-safe failure with a stable public reason."""

    def __init__(self, reason: str):
        if reason not in ERROR_REASONS:
            reason = "unexpected_error"
        super().__init__(reason)
        self.reason = reason


class AdapterOperationError(RuntimeError):
    """Adapter-raised fixed failure.  No raw remote message may be included."""

    def __init__(self, reason: str):
        if reason not in ERROR_REASONS:
            reason = "unexpected_error"
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RichCoreContractDescriptor:
    adapter_version: str
    slot_protocol: str
    full_session_protocol: str
    root_current_protocol: str
    operations: tuple[str, ...]


RICH_CORE_OPERATIONS = (
    "open_slot",
    "begin_full_rebuild",
    "assert_canonical_lock_held",
    "verify_immutable_baseline",
    "collect_fresh_inventory",
    "prove_permissions",
    "acquire_run_resources",
    "recover_sealed_entry",
    "recover_sealed_round",
    "collect_and_stage_full_snapshot",
    "reserve_full_stage_assets",
    "install_full_pass_evidence",
    "seal_full_entry",
    "seal_round",
    "finalize_full_rebuild",
    "publish_root_run_current",
    "inspect_root_run_current",
    "inspect_committed_full_rebuild",
    "publish_compatibility_currents",
    "describe_capability",
)
SUPPORTED_RICH_CORE_CONTRACT = RichCoreContractDescriptor(
    adapter_version=RICH_CORE_ADAPTER_VERSION,
    slot_protocol=RICH_SLOT_PROTOCOL,
    full_session_protocol=RICH_FULL_SESSION_PROTOCOL,
    root_current_protocol=RICH_ROOT_CURRENT_PROTOCOL,
    operations=RICH_CORE_OPERATIONS,
)


class CoreCapability:
    """Nominal opaque port.  Coordinator code never constructs capabilities."""

    __slots__ = ()

    def __reduce__(self):  # pragma: no cover - concrete core enforces stronger rules
        raise TypeError("runtime rich-core capabilities are not serializable")


class InventoryCapabilityV3(CoreCapability):
    pass


class PermissionCapabilityV2(CoreCapability):
    pass


class BaselineCapabilityV2(CoreCapability):
    pass


class RunResourcesCapabilityV2(CoreCapability):
    pass


class FullStageCapabilityV2(CoreCapability):
    pass


class ReservedFullStageCapabilityV2(CoreCapability):
    pass


class PreparedFullStageCapabilityV2(CoreCapability):
    pass


class SealedFullEntryCapabilityV2(CoreCapability):
    pass


class FullRoundCapabilityV2(CoreCapability):
    pass


class FullRunReadyCapabilityV2(CoreCapability):
    pass


class RootCurrentCapabilityV2(CoreCapability):
    pass


class RootCommitGrantV2(CoreCapability):
    pass


class CommittedRunInspectionCapabilityV2(CoreCapability):
    pass


@dataclass(frozen=True)
class EntryBindingV2:
    channel_id: str
    relative_path: str
    normalized_relative_path: str
    entry_type: str
    parent_channel_id: str | None
    inventory_class: str

    def audit_record(self) -> dict[str, Any]:
        return {
            "channelId": self.channel_id,
            "relativePath": self.relative_path,
            "normalizedRelativePath": self.normalized_relative_path,
            "type": self.entry_type,
            "parentChannelId": self.parent_channel_id,
            "inventoryClass": self.inventory_class,
        }


@dataclass(frozen=True)
class RuntimeComponentBindingV1:
    name: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class ApprovedRebuildBaselineV2:
    """Immutable configuration authority selected before a run is created.

    ``TEST_ONLY`` values support dependency injection without weakening the
    production loader.  Only the integrity-bound loader can attach the private
    production marker; production execution rejects every other source.
    """

    schema_version: str
    authority_mode: str
    archive_root: Path
    state_path: Path
    queue_path: Path
    baseline_dir: Path
    baseline_sha256: str
    expected_state_sha256: str
    expected_queue_sha256: str
    expected_entry_count: int
    expected_entry_set_sha256: str
    expected_entries: tuple[EntryBindingV2, ...]
    guild_id: str
    timezone_name: str
    adapter_code_sha256: str
    rich_core_code_sha256: str
    coordinator_code_sha256: str
    configuration_sha256: str
    expected_entries_artifact_sha256: str
    runtime_manifest_path: Path | None
    runtime_manifest_sha256: str
    component_bindings: tuple[RuntimeComponentBindingV1, ...]
    limits: "FullRebuildLimitsV2"
    _authority: object | None = field(default=None, repr=False, compare=False)

    @property
    def production_authorized(self) -> bool:
        return self._authority is _PRODUCTION_AUTHORITY


@dataclass(frozen=True)
class FullRebuildLimitsV2:
    max_convergence_rounds: int = 8
    max_runtime_seconds: int = 86_400
    max_requests: int = 200_000
    max_retries: int = 10_000
    max_asset_files: int = 1_000_000
    max_asset_bytes: int = 1_099_511_627_776
    minimum_free_space_bytes: int = 10_737_418_240

    def as_tuple(self) -> tuple[tuple[str, int], ...]:
        return (
            ("maxConvergenceRounds", self.max_convergence_rounds),
            ("maxRuntimeSeconds", self.max_runtime_seconds),
            ("maxRequests", self.max_requests),
            ("maxRetries", self.max_retries),
            ("maxAssetFiles", self.max_asset_files),
            ("maxAssetBytes", self.max_asset_bytes),
            ("minimumFreeSpaceBytes", self.minimum_free_space_bytes),
        )


@dataclass(frozen=True)
class FullRebuildConfigV2:
    run_id: str
    approved_baseline: ApprovedRebuildBaselineV2

    def __getattr__(self, name: str) -> Any:
        # Keep request-building call sites narrow while ensuring every value is
        # sourced from one immutable approved baseline, never duplicated CLI
        # fields that can drift independently.
        baseline = object.__getattribute__(self, "approved_baseline")
        if hasattr(baseline, name):
            return getattr(baseline, name)
        raise AttributeError(name)


@dataclass(frozen=True)
class FullRebuildBeginRequestV3:
    schema_version: str
    run_id: str
    archive_root: Path
    expected_entry_count: int
    expected_entry_set_sha256: str
    expected_entries: tuple[EntryBindingV2, ...]
    guild_id: str
    timezone_name: str
    baseline_sha256: str
    state_sha256: str
    queue_sha256: str
    adapter_code_sha256: str
    rich_core_code_sha256: str
    coordinator_code_sha256: str
    configuration_sha256: str
    expected_entries_artifact_sha256: str
    runtime_manifest_sha256: str
    limits: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class BaselineVerificationRequestV2:
    run_id: str
    baseline_dir: Path
    expected_baseline_sha256: str
    expected_state_sha256: str
    expected_queue_sha256: str


@dataclass(frozen=True)
class InventoryRequestV3:
    run_id: str
    round_id: str
    round_kind: str
    guild_id: str
    expected_entry_count: int
    expected_entry_set_sha256: str
    expected_entries: tuple[EntryBindingV2, ...]


@dataclass(frozen=True)
class PermissionRequestV2:
    run_id: str
    round_id: str
    guild_id: str
    expected_entry_count: int
    expected_entry_set_sha256: str


@dataclass(frozen=True)
class ResourceReservationRequestV2:
    run_id: str
    archive_root: Path
    max_asset_files: int
    max_asset_bytes: int
    minimum_free_space_bytes: int
    resume_audit_sha256: str | None


@dataclass(frozen=True)
class EntryStageRequestV3:
    run_id: str
    round_id: str
    round_kind: str
    entry: EntryBindingV2
    sequence: int


@dataclass(frozen=True)
class ResumeSealedEntryRequestV2:
    run_id: str
    round_id: str
    round_kind: str
    entry: EntryBindingV2
    sealed_audit_sha256: str


@dataclass(frozen=True)
class RoundSealRequestV2:
    run_id: str
    round_id: str
    round_kind: str
    sequence: int
    expected_entry_count: int
    expected_entry_set_sha256: str
    sealed_audit_sha256_by_channel: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ResumeRoundRequestV2:
    run_id: str
    round_id: str
    round_kind: str
    round_audit_sha256: str


@dataclass(frozen=True)
class FinalizeFullRebuildRequestV2:
    run_id: str
    expected_entry_count: int
    expected_entry_set_sha256: str
    state_sha256: str
    queue_sha256: str
    round_audit_sha256s: tuple[str, ...]
    zero_round_streak: int


@dataclass(frozen=True)
class RootPublishRequestV2:
    run_id: str
    archive_root: Path
    expected_entry_count: int
    expected_entry_set_sha256: str
    state_sha256: str
    queue_sha256: str


@dataclass(frozen=True)
class CompatibilityPublishRequestV2:
    run_id: str
    expected_entry_count: int
    expected_entry_set_sha256: str


@dataclass(frozen=True)
class CommittedRunInspectionRequestV2:
    run_id: str
    expected_entry_count: int
    expected_entry_set_sha256: str
    run_manifest_sha256: str
    full_run_binding_sha256: str
    prior_pointer_sha256: str | None
    new_pointer_sha256: str
    final_receipt_sha256: str


class FullRebuildSessionV2:
    """Nominal full-run port.  Base methods always fail closed."""

    contract = SUPPORTED_RICH_CORE_CONTRACT

    def assert_canonical_lock_held(self) -> None:
        raise AdapterOperationError("rich_core_contract_pending")

    def verify_immutable_baseline(
        self, request: BaselineVerificationRequestV2
    ) -> BaselineCapabilityV2:
        del request
        raise AdapterOperationError("rich_core_contract_pending")

    def collect_fresh_inventory(
        self, request: InventoryRequestV3
    ) -> InventoryCapabilityV3:
        del request
        raise AdapterOperationError("rich_core_contract_pending")

    def prove_permissions(
        self, request: PermissionRequestV2, *, inventory: InventoryCapabilityV3
    ) -> PermissionCapabilityV2:
        del request, inventory
        raise AdapterOperationError("rich_core_contract_pending")

    def acquire_run_resources(
        self,
        request: ResourceReservationRequestV2,
        *,
        baseline: BaselineCapabilityV2,
        inventory: InventoryCapabilityV3,
    ) -> RunResourcesCapabilityV2:
        del request, baseline, inventory
        raise AdapterOperationError("rich_core_contract_pending")

    def recover_sealed_entry(
        self,
        request: ResumeSealedEntryRequestV2,
        *,
        prior_round: FullRoundCapabilityV2 | None,
        inventory: InventoryCapabilityV3,
        permissions: PermissionCapabilityV2,
        resources: RunResourcesCapabilityV2,
    ) -> SealedFullEntryCapabilityV2:
        del request, prior_round, inventory, permissions, resources
        raise AdapterOperationError("rich_core_contract_pending")

    def recover_sealed_round(
        self,
        request: ResumeRoundRequestV2,
        *,
        resources: RunResourcesCapabilityV2,
    ) -> FullRoundCapabilityV2:
        del request, resources
        raise AdapterOperationError("rich_core_contract_pending")

    def collect_and_stage_full_snapshot(
        self,
        request: EntryStageRequestV3,
        *,
        prior_round: FullRoundCapabilityV2 | None,
        inventory: InventoryCapabilityV3,
        permissions: PermissionCapabilityV2,
        resources: RunResourcesCapabilityV2,
    ) -> FullStageCapabilityV2:
        del request, prior_round, inventory, permissions, resources
        raise AdapterOperationError("rich_core_contract_pending")

    def reserve_full_stage_assets(
        self,
        stage: FullStageCapabilityV2,
        *,
        resources: RunResourcesCapabilityV2,
    ) -> ReservedFullStageCapabilityV2:
        del stage, resources
        raise AdapterOperationError("rich_core_contract_pending")

    def install_full_pass_evidence(
        self,
        reserved: ReservedFullStageCapabilityV2,
        *,
        inventory: InventoryCapabilityV3,
        permissions: PermissionCapabilityV2,
    ) -> PreparedFullStageCapabilityV2:
        del reserved, inventory, permissions
        raise AdapterOperationError("rich_core_contract_pending")

    def seal_full_entry(
        self, prepared: PreparedFullStageCapabilityV2
    ) -> SealedFullEntryCapabilityV2:
        del prepared
        raise AdapterOperationError("rich_core_contract_pending")

    def seal_round(
        self,
        request: RoundSealRequestV2,
        *,
        inventory: InventoryCapabilityV3,
        permissions: PermissionCapabilityV2,
        sealed_entries: tuple[SealedFullEntryCapabilityV2, ...],
    ) -> FullRoundCapabilityV2:
        del request, inventory, permissions, sealed_entries
        raise AdapterOperationError("rich_core_contract_pending")

    def finalize_full_rebuild(
        self,
        request: FinalizeFullRebuildRequestV2,
        *,
        rounds: tuple[FullRoundCapabilityV2, ...],
        resources: RunResourcesCapabilityV2,
    ) -> FullRunReadyCapabilityV2:
        del request, rounds, resources
        raise AdapterOperationError("rich_core_contract_pending")

    def publish_root_run_current(
        self, request: RootPublishRequestV2, *, ready: FullRunReadyCapabilityV2
    ) -> RootCurrentCapabilityV2:
        del request, ready
        raise AdapterOperationError("rich_core_contract_pending")

    def inspect_root_run_current(
        self,
        request: RootPublishRequestV2,
        *,
        ready: FullRunReadyCapabilityV2,
        published: RootCurrentCapabilityV2,
    ) -> RootCommitGrantV2:
        del request, ready, published
        raise AdapterOperationError("rich_core_contract_pending")

    def inspect_committed_full_rebuild(
        self, request: CommittedRunInspectionRequestV2
    ) -> CommittedRunInspectionCapabilityV2:
        del request
        raise AdapterOperationError("rich_core_contract_pending")

    def publish_compatibility_currents(
        self,
        request: CompatibilityPublishRequestV2,
        *,
        grant: RootCommitGrantV2,
    ) -> Mapping[str, Any]:
        del request, grant
        raise AdapterOperationError("rich_core_contract_pending")

    def describe_capability(self, capability: CoreCapability) -> Mapping[str, Any]:
        del capability
        raise AdapterOperationError("rich_core_contract_pending")


class LockedRichSlotV3:
    contract = SUPPORTED_RICH_CORE_CONTRACT

    def begin_full_rebuild(
        self, request: FullRebuildBeginRequestV3
    ) -> AbstractContextManager[FullRebuildSessionV2]:
        del request
        raise AdapterOperationError("rich_core_contract_pending")


class ManagedRichCoreAdapterV3:
    contract = SUPPORTED_RICH_CORE_CONTRACT

    def open_slot(
        self, *, archive_root: Path, lock_path: Path
    ) -> AbstractContextManager[LockedRichSlotV3]:
        del archive_root, lock_path
        raise AdapterOperationError("rich_core_contract_pending")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FullRebuildError("journal_corrupt") from exc


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_hash(value: Any, reason: str = "journal_corrupt") -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise FullRebuildError(reason)
    return value


def _reject_controls(value: str) -> None:
    if any(
        unicodedata.category(character) == "Cc"
        or ord(character) in BIDI_CONTROL_CODEPOINTS
        for character in value
    ):
        raise FullRebuildError("unsafe_input_path")


def _lexical_absolute(path: str | Path) -> Path:
    text = str(path)
    _reject_controls(text)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise FullRebuildError("unsafe_input_path")
    absolute = Path(os.path.abspath(candidate))
    if absolute == Path(absolute.anchor):
        raise FullRebuildError("unsafe_input_path")
    return absolute


def _reject_symlink_components(path: Path, *, allow_missing_tail: bool = False) -> None:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    missing = False
    for part in absolute.parts[1:]:
        current /= part
        if missing:
            continue
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing_tail:
                missing = True
                continue
            raise FullRebuildError("unsafe_input_path")
        if stat.S_ISLNK(info.st_mode):
            raise FullRebuildError("unsafe_input_path")


def _require_safe_directory(path: Path) -> Path:
    absolute = _lexical_absolute(path)
    _reject_symlink_components(absolute)
    info = absolute.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink < 1
        or info.st_mode & stat.S_IWOTH
    ):
        raise FullRebuildError("unsafe_input_path")
    return absolute


def _require_safe_file(path: Path) -> Path:
    absolute = _lexical_absolute(path)
    _reject_symlink_components(absolute)
    info = absolute.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & stat.S_IWOTH
    ):
        raise FullRebuildError("unsafe_input_path")
    return absolute


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_mkdirs(path: Path, *, existing_root: Path) -> None:
    root = _require_safe_directory(existing_root)
    target = _lexical_absolute(path)
    if not _contains(root, target):
        raise FullRebuildError("unsafe_input_path")
    current = root
    for part in target.relative_to(root).parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            _fsync_directory(current.parent)
            info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise FullRebuildError("unsafe_input_path")


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    _reject_symlink_components(path, allow_missing_tail=True)
    if os.path.lexists(path) and path.is_symlink():
        raise FullRebuildError("unsafe_input_path")
    data = canonical_json_bytes(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _exclusive_private_json(path: Path, value: Mapping[str, Any]) -> None:
    data = canonical_json_bytes(value) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _entry_from_mapping(value: Mapping[str, Any]) -> EntryBindingV2:
    if not isinstance(value, Mapping):
        raise FullRebuildError("invalid_expected_entries")
    channel_id = value.get("channelId")
    relative_path = value.get("relativePath")
    entry_type = value.get("type")
    parent_channel_id = value.get("parentChannelId")
    inventory_class = value.get("inventoryClass")
    if (
        not isinstance(channel_id, str)
        or not SNOWFLAKE_RE.fullmatch(channel_id)
        or not isinstance(relative_path, str)
        or not relative_path
        or relative_path != relative_path.strip()
        or not isinstance(entry_type, str)
        or not entry_type
        or parent_channel_id is not None
        and (not isinstance(parent_channel_id, str) or not SNOWFLAKE_RE.fullmatch(parent_channel_id))
        or inventory_class not in REQUIRED_INVENTORY_CLASSES
    ):
        raise FullRebuildError("invalid_expected_entries")
    _reject_controls(relative_path)
    pure = PurePosixPath(relative_path)
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or "\\" in relative_path
        or relative_path.endswith("/")
    ):
        raise FullRebuildError("invalid_expected_entries")
    normalized = unicodedata.normalize("NFKC", relative_path).casefold()
    supplied_normalized = value.get("normalizedRelativePath")
    if supplied_normalized is not None and supplied_normalized != normalized:
        raise FullRebuildError("invalid_expected_entries")
    return EntryBindingV2(
        channel_id=channel_id,
        relative_path=relative_path,
        normalized_relative_path=normalized,
        entry_type=entry_type,
        parent_channel_id=parent_channel_id,
        inventory_class=inventory_class,
    )


def validate_expected_entries(
    values: Sequence[Mapping[str, Any]], *, expected_count: int
) -> tuple[EntryBindingV2, ...]:
    if (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count < 1
        or not isinstance(values, Sequence)
        or isinstance(values, (str, bytes, bytearray))
    ):
        raise FullRebuildError("invalid_expected_entries")
    entries = tuple(_entry_from_mapping(value) for value in values)
    if len(entries) != expected_count:
        raise FullRebuildError("expected_entry_count_mismatch")
    channel_ids = [entry.channel_id for entry in entries]
    normalized_paths = [entry.normalized_relative_path for entry in entries]
    if len(set(channel_ids)) != len(channel_ids) or len(set(normalized_paths)) != len(normalized_paths):
        raise FullRebuildError("invalid_expected_entries")
    return tuple(sorted(entries, key=lambda entry: int(entry.channel_id)))


def entry_set_sha256(entries: Sequence[EntryBindingV2]) -> str:
    return json_sha256([entry.audit_record() for entry in entries])


def _validate_limits(limits: FullRebuildLimitsV2) -> None:
    values = dict(limits.as_tuple())
    if (
        not 1 <= values["maxConvergenceRounds"] <= 64
        or not 60 <= values["maxRuntimeSeconds"] <= 604_800
        or not 1 <= values["maxRequests"] <= 10_000_000
        or not 0 <= values["maxRetries"] <= 1_000_000
        or not 0 <= values["maxAssetFiles"] <= 10_000_000
        or not 0 <= values["maxAssetBytes"] <= 10 * 1024**4
        or not 0 <= values["minimumFreeSpaceBytes"] <= 10 * 1024**4
    ):
        raise FullRebuildError("rich_asset_budget_exhausted")


def _require_integrity_file(path: Path) -> Path:
    safe = _require_safe_file(path)
    info = safe.lstat()
    if info.st_mode & 0o022 or info.st_nlink != 1:
        raise FullRebuildError("rich_core_integrity_mismatch")
    return safe


def _require_integrity_component_path(*, skill_root: Path, path: Path) -> Path:
    root = _require_safe_directory(skill_root)
    root_info = root.lstat()
    if root_info.st_mode & 0o022:
        raise FullRebuildError("rich_core_integrity_mismatch")
    candidate = _lexical_absolute(path)
    if not _contains(root, candidate) or candidate == root:
        raise FullRebuildError("rich_core_integrity_mismatch")
    current = root
    parts = candidate.relative_to(root).parts
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise FullRebuildError("rich_core_integrity_mismatch") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or index < len(parts) - 1
            and not stat.S_ISDIR(info.st_mode)
        ):
            raise FullRebuildError("rich_core_integrity_mismatch")
    return _require_integrity_file(candidate)


def _validate_approved_baseline(
    baseline: ApprovedRebuildBaselineV2,
) -> ApprovedRebuildBaselineV2:
    if (
        not isinstance(baseline, ApprovedRebuildBaselineV2)
        or baseline.schema_version != APPROVED_BASELINE_SCHEMA
        or baseline.authority_mode not in {"TEST_ONLY", "INTEGRITY_BOUND_V1"}
    ):
        raise FullRebuildError("rich_core_contract_unsupported")
    production = baseline.production_authorized
    if production != (baseline.authority_mode == "INTEGRITY_BOUND_V1"):
        raise FullRebuildError("rich_core_integrity_mismatch")
    if not production and baseline.component_bindings:
        raise FullRebuildError("rich_core_integrity_mismatch")

    archive_root = _require_safe_directory(baseline.archive_root)
    state_path = _require_safe_file(baseline.state_path)
    queue_path = _require_safe_file(baseline.queue_path)
    baseline_dir = _require_safe_directory(baseline.baseline_dir)
    if state_path == queue_path:
        raise FullRebuildError("unsafe_input_path")
    if (
        _contains(archive_root, baseline_dir)
        or _contains(baseline_dir, archive_root)
        or _contains(archive_root, state_path)
        or _contains(archive_root, queue_path)
        or _contains(baseline_dir, state_path)
        or _contains(baseline_dir, queue_path)
    ):
        raise FullRebuildError("unsafe_input_path")
    for value in (
        baseline.baseline_sha256,
        baseline.expected_state_sha256,
        baseline.expected_queue_sha256,
        baseline.expected_entry_set_sha256,
        baseline.adapter_code_sha256,
        baseline.rich_core_code_sha256,
        baseline.coordinator_code_sha256,
        baseline.configuration_sha256,
        baseline.expected_entries_artifact_sha256,
        baseline.runtime_manifest_sha256,
    ):
        _require_hash(value, "invalid_expected_entries")
    if (
        not isinstance(baseline.guild_id, str)
        or not SNOWFLAKE_RE.fullmatch(baseline.guild_id)
        or not isinstance(baseline.timezone_name, str)
        or not baseline.timezone_name.strip()
    ):
        raise FullRebuildError("invalid_expected_entries")
    _reject_controls(baseline.timezone_name)
    entries = validate_expected_entries(
        [entry.audit_record() for entry in baseline.expected_entries],
        expected_count=baseline.expected_entry_count,
    )
    if entry_set_sha256(entries) != baseline.expected_entry_set_sha256:
        raise FullRebuildError("expected_entry_set_digest_mismatch")
    _validate_limits(baseline.limits)

    bindings: tuple[RuntimeComponentBindingV1, ...] = ()
    manifest_path: Path | None = None
    if production:
        if baseline.runtime_manifest_path is None:
            raise FullRebuildError("rich_core_integrity_mismatch")
        manifest_path = _require_integrity_file(baseline.runtime_manifest_path)
        if file_sha256(manifest_path) != baseline.runtime_manifest_sha256:
            raise FullRebuildError("rich_core_integrity_mismatch")
        if {binding.name for binding in baseline.component_bindings} != set(
            RUNTIME_COMPONENT_PATHS
        ):
            raise FullRebuildError("rich_core_integrity_mismatch")
        checked: list[RuntimeComponentBindingV1] = []
        for binding in baseline.component_bindings:
            if not isinstance(binding, RuntimeComponentBindingV1):
                raise FullRebuildError("rich_core_integrity_mismatch")
            expected_suffix = PurePosixPath(RUNTIME_COMPONENT_PATHS[binding.name])
            safe_path = _require_integrity_file(binding.path)
            if tuple(safe_path.parts[-len(expected_suffix.parts):]) != expected_suffix.parts:
                raise FullRebuildError("rich_core_integrity_mismatch")
            _require_hash(binding.sha256, "rich_core_integrity_mismatch")
            if file_sha256(safe_path) != binding.sha256:
                raise FullRebuildError("rich_core_integrity_mismatch")
            checked.append(RuntimeComponentBindingV1(binding.name, safe_path, binding.sha256))
        bindings = tuple(sorted(checked, key=lambda item: item.name))

    return ApprovedRebuildBaselineV2(
        **{
            **baseline.__dict__,
            "archive_root": archive_root,
            "state_path": state_path,
            "queue_path": queue_path,
            "baseline_dir": baseline_dir,
            "expected_entries": entries,
            "runtime_manifest_path": manifest_path,
            "component_bindings": bindings,
        }
    )


def validate_config(config: FullRebuildConfigV2) -> FullRebuildConfigV2:
    if not isinstance(config.run_id, str) or not RUN_ID_RE.fullmatch(config.run_id):
        raise FullRebuildError("invalid_run_id")
    _reject_controls(config.run_id)
    return FullRebuildConfigV2(
        run_id=config.run_id,
        approved_baseline=_validate_approved_baseline(config.approved_baseline),
    )


def _assert_canonical_lock(session: FullRebuildSessionV2) -> None:
    result = _call_adapter(session.assert_canonical_lock_held)
    if result is not None:
        raise FullRebuildError("rich_core_authority_invalid")


def _canonical_config_audit(baseline: ApprovedRebuildBaselineV2) -> dict[str, Any]:
    return {
        "schemaVersion": "openclaw-discord-full-rich-rebuild-config.v1",
        "archiveRoot": str(baseline.archive_root),
        "statePath": str(baseline.state_path),
        "queuePath": str(baseline.queue_path),
        "baselineDir": str(baseline.baseline_dir),
        "baselineSha256": baseline.baseline_sha256,
        "stateSha256": baseline.expected_state_sha256,
        "queueSha256": baseline.expected_queue_sha256,
        "expectedEntryCount": baseline.expected_entry_count,
        "expectedEntrySetSha256": baseline.expected_entry_set_sha256,
        "expectedEntriesArtifactSha256": baseline.expected_entries_artifact_sha256,
        "guildId": baseline.guild_id,
        "timezone": baseline.timezone_name,
        "limits": dict(baseline.limits.as_tuple()),
    }


def _expected_entries_artifact_audit(
    baseline: ApprovedRebuildBaselineV2,
) -> dict[str, Any]:
    return {
        "schemaVersion": "openclaw-discord-full-rich-rebuild-entries.v1",
        "entryCount": baseline.expected_entry_count,
        "entrySetSha256": baseline.expected_entry_set_sha256,
        "entries": [entry.audit_record() for entry in baseline.expected_entries],
    }


def _verify_runtime_authority_under_lock(
    config: FullRebuildConfigV2,
    *,
    session: FullRebuildSessionV2,
) -> None:
    """Recheck the production trust bundle only after lock acquisition."""

    _assert_canonical_lock(session)
    baseline = config.approved_baseline
    if not baseline.production_authorized:
        return
    bindings = {binding.name: binding for binding in baseline.component_bindings}
    if set(bindings) != set(RUNTIME_COMPONENT_PATHS):
        raise FullRebuildError("rich_core_integrity_mismatch")
    assert baseline.runtime_manifest_path is not None
    skill_root = bindings["coordinator"].path.parent.parent
    manifest_file = _require_integrity_component_path(
        skill_root=skill_root,
        path=baseline.runtime_manifest_path,
    )
    if file_sha256(manifest_file) != baseline.runtime_manifest_sha256:
        raise FullRebuildError("rich_core_integrity_mismatch")
    expected_hashes = {
        "adapter": baseline.adapter_code_sha256,
        "richCore": baseline.rich_core_code_sha256,
        "coordinator": baseline.coordinator_code_sha256,
        "dailyRunner": bindings["dailyRunner"].sha256,
        "managedComponent": bindings["managedComponent"].sha256,
        "canonicalConfig": baseline.configuration_sha256,
        "expectedEntries": baseline.expected_entries_artifact_sha256,
    }
    for name, expected_hash in expected_hashes.items():
        binding = bindings[name]
        safe = _require_integrity_component_path(
            skill_root=skill_root,
            path=binding.path,
        )
        actual_hash = file_sha256(safe)
        if binding.sha256 != expected_hash or actual_hash != expected_hash:
            raise FullRebuildError("rich_core_integrity_mismatch")
    coordinator = bindings["coordinator"].path
    current = _require_integrity_component_path(
        skill_root=skill_root,
        path=Path(__file__),
    )
    coordinator_info = coordinator.stat()
    current_info = current.stat()
    if (coordinator_info.st_dev, coordinator_info.st_ino) != (
        current_info.st_dev,
        current_info.st_ino,
    ):
        raise FullRebuildError("rich_core_integrity_mismatch")
    canonical_config = _load_json(
        bindings["canonicalConfig"].path, "rich_core_integrity_mismatch"
    )
    expected_entries = _load_json(
        bindings["expectedEntries"].path, "rich_core_integrity_mismatch"
    )
    if canonical_config != _canonical_config_audit(baseline):
        raise FullRebuildError("rich_core_integrity_mismatch")
    if expected_entries != _expected_entries_artifact_audit(baseline):
        raise FullRebuildError("rich_core_integrity_mismatch")


def _exact_contract(value: Any) -> bool:
    return (
        type(getattr(value, "contract", None)) is RichCoreContractDescriptor
        and value.contract == SUPPORTED_RICH_CORE_CONTRACT
    )


def require_adapter(value: Any) -> ManagedRichCoreAdapterV3:
    if not isinstance(value, ManagedRichCoreAdapterV3) or not _exact_contract(value):
        raise FullRebuildError("rich_core_contract_unsupported")
    return value


def require_slot(value: Any) -> LockedRichSlotV3:
    if not isinstance(value, LockedRichSlotV3) or not _exact_contract(value):
        raise FullRebuildError("rich_core_contract_unsupported")
    return value


def require_session(value: Any) -> FullRebuildSessionV2:
    if not isinstance(value, FullRebuildSessionV2) or not _exact_contract(value):
        raise FullRebuildError("rich_core_contract_unsupported")
    return value


CAPABILITY_TYPES: dict[str, type[CoreCapability]] = {
    "inventory": InventoryCapabilityV3,
    "permissions": PermissionCapabilityV2,
    "baseline": BaselineCapabilityV2,
    "resources": RunResourcesCapabilityV2,
    "stage": FullStageCapabilityV2,
    "reserved_stage": ReservedFullStageCapabilityV2,
    "prepared_stage": PreparedFullStageCapabilityV2,
    "sealed_entry": SealedFullEntryCapabilityV2,
    "round": FullRoundCapabilityV2,
    "ready": FullRunReadyCapabilityV2,
    "root_current": RootCurrentCapabilityV2,
    "root_grant": RootCommitGrantV2,
    "committed_inspection": CommittedRunInspectionCapabilityV2,
}


class CapabilityTracker:
    """Reject output capability object reuse while retaining strong references."""

    def __init__(self) -> None:
        self._seen: dict[int, CoreCapability] = {}

    def accept(
        self,
        value: Any,
        kind: str,
        *,
        reusable: bool = False,
    ) -> CoreCapability:
        expected = CAPABILITY_TYPES[kind]
        if not isinstance(value, expected):
            raise FullRebuildError("rich_core_authority_invalid")
        identity = id(value)
        if not reusable and identity in self._seen:
            raise FullRebuildError("rich_core_authority_replayed")
        self._seen.setdefault(identity, value)
        return value


def _safe_read_bytes(path: Path) -> bytes:
    safe = _require_safe_file(path)
    descriptor = os.open(safe, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        data = b""
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_nlink != 1
        ):
            raise FullRebuildError("state_queue_drift")
        return data
    finally:
        os.close(descriptor)


def _assert_state_queue(
    config: FullRebuildConfigV2, *, state_sha256: str, queue_sha256: str
) -> None:
    if bytes_sha256(_safe_read_bytes(config.state_path)) != state_sha256:
        raise FullRebuildError("state_queue_drift")
    if bytes_sha256(_safe_read_bytes(config.queue_path)) != queue_sha256:
        raise FullRebuildError("state_queue_drift")


def _envelope(payload: Mapping[str, Any], schema: str) -> dict[str, Any]:
    copied = deepcopy(dict(payload))
    return {
        "schemaVersion": schema,
        "payload": copied,
        "payloadSha256": json_sha256(copied),
    }


def _load_envelope(path: Path, schema: str, reason: str) -> dict[str, Any]:
    safe = _require_safe_file(path)
    try:
        value = json.loads(safe.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullRebuildError(reason) from exc
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != schema
        or not isinstance(value.get("payload"), dict)
        or value.get("payloadSha256") != json_sha256(value["payload"])
    ):
        raise FullRebuildError(reason)
    return value["payload"]


def _phase_rank(phase: str) -> int:
    if phase not in PHASES:
        raise FullRebuildError("journal_corrupt")
    return PHASES.index(phase)


class DurableJournal:
    """Receipt-first audit journal.  Orphan receipts are retained on crashes."""

    def __init__(
        self,
        *,
        archive_root: Path,
        run_id: str,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.run_root = archive_root / "runs" / run_id
        self.receipt_root = self.run_root / "receipts" / "coordinator"
        self.events_root = self.receipt_root / "events"
        self.journal_path = self.run_root / "run-journal.json"
        self.final_receipt_path = self.receipt_root / "full-rebuild-run.json"
        self._archive_root = archive_root
        self._fault = fault_injector or (lambda _point: None)

    def ensure_layout(self) -> None:
        _secure_mkdirs(self.events_root, existing_root=self._archive_root)

    def exists(self) -> bool:
        return os.path.lexists(self.journal_path)

    def load(self) -> dict[str, Any]:
        payload = _load_envelope(
            self.journal_path, JOURNAL_ENVELOPE_SCHEMA, "journal_corrupt"
        )
        self._validate_receipt_chain(payload)
        return payload

    def create(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        if self.exists():
            raise FullRebuildError("journal_binding_mismatch")
        journal = {
            "schemaVersion": JOURNAL_SCHEMA,
            "coordinatorVersion": COORDINATOR_SCHEMA,
            **deepcopy(dict(binding)),
            "phase": "PREPARED",
            "resumePhase": None,
            "sequence": 0,
            "receiptChain": [],
            "baselineAuditSha256": None,
            "resourceAuditSha256": None,
            "rounds": [],
            "activeRound": None,
            "zeroRoundStreak": 0,
            "root": {},
            "lastError": None,
        }
        self._write(journal)
        return journal

    def _write(self, journal: Mapping[str, Any]) -> None:
        self._fault("before_journal_write")
        _atomic_private_json(
            self.journal_path,
            _envelope(journal, JOURNAL_ENVELOPE_SCHEMA),
        )
        self._fault("after_journal_write")

    def record(
        self,
        journal: Mapping[str, Any],
        *,
        event: str,
        payload: Mapping[str, Any],
        update: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        sequence = int(journal["sequence"]) + 1
        previous = (
            journal["receiptChain"][-1]["sha256"]
            if journal["receiptChain"]
            else None
        )
        receipt_payload = {
            "schemaVersion": EVENT_RECEIPT_SCHEMA,
            "sequence": sequence,
            "event": event,
            "previousReceiptSha256": previous,
            "payload": deepcopy(dict(payload)),
            "payloadSha256": json_sha256(payload),
        }
        receipt_name = (
            f"{sequence:08d}-{event}-{json_sha256(receipt_payload)[:16]}-"
            f"{secrets.token_hex(4)}.json"
        )
        receipt_path = self.events_root / receipt_name
        self._fault(f"before_receipt:{event}")
        _exclusive_private_json(receipt_path, receipt_payload)
        receipt_sha = file_sha256(receipt_path)
        self._fault(f"after_receipt:{event}")
        changed = deepcopy(dict(journal))
        changed["sequence"] = sequence
        changed["receiptChain"].append({
            "sequence": sequence,
            "event": event,
            "fileName": receipt_name,
            "sha256": receipt_sha,
        })
        update(changed)
        self._write(changed)
        return changed

    def write_final_receipt(self, payload: Mapping[str, Any]) -> None:
        value = {
            "schemaVersion": FINAL_RECEIPT_SCHEMA,
            "status": "AUDIT_ONLY",
            **deepcopy(dict(payload)),
        }
        if os.path.lexists(self.final_receipt_path):
            existing = _require_safe_file(self.final_receipt_path).read_bytes()
            candidate = canonical_json_bytes(value) + b"\n"
            if existing != candidate:
                raise FullRebuildError("receipt_chain_corrupt")
            return
        _exclusive_private_json(self.final_receipt_path, value)

    def load_final_receipt(self) -> tuple[dict[str, Any], str]:
        safe = _require_safe_file(self.final_receipt_path)
        raw = _safe_read_bytes(safe)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FullRebuildError("committed_readback_failed") from exc
        if not isinstance(value, dict):
            raise FullRebuildError("committed_readback_failed")
        return value, bytes_sha256(raw)

    def _validate_receipt_chain(self, journal: Mapping[str, Any]) -> None:
        chain = journal.get("receiptChain")
        if not isinstance(chain, list) or journal.get("sequence") != len(chain):
            raise FullRebuildError("receipt_chain_corrupt")
        previous: str | None = None
        for expected_sequence, reference in enumerate(chain, start=1):
            if not isinstance(reference, dict):
                raise FullRebuildError("receipt_chain_corrupt")
            name = reference.get("fileName")
            if (
                reference.get("sequence") != expected_sequence
                or not isinstance(name, str)
                or Path(name).name != name
                or "/" in name
            ):
                raise FullRebuildError("receipt_chain_corrupt")
            path = self.events_root / name
            if file_sha256(_require_safe_file(path)) != reference.get("sha256"):
                raise FullRebuildError("receipt_chain_corrupt")
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FullRebuildError("receipt_chain_corrupt") from exc
            if (
                not isinstance(receipt, dict)
                or receipt.get("schemaVersion") != EVENT_RECEIPT_SCHEMA
                or receipt.get("sequence") != expected_sequence
                or receipt.get("event") != reference.get("event")
                or receipt.get("previousReceiptSha256") != previous
                or not isinstance(receipt.get("payload"), dict)
                or receipt.get("payloadSha256") != json_sha256(receipt["payload"])
            ):
                raise FullRebuildError("receipt_chain_corrupt")
            previous = reference["sha256"]


def _exact_keys(
    value: Mapping[str, Any], required: set[str], *, optional: set[str] | None = None
) -> None:
    allowed = required | (optional or set())
    if set(value) != required and not (
        required.issubset(value) and set(value).issubset(allowed)
    ):
        raise FullRebuildError("rich_core_authority_invalid")


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise FullRebuildError("rich_core_authority_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FullRebuildError("rich_core_authority_invalid") from exc
    if parsed.tzinfo is None:
        raise FullRebuildError("rich_core_authority_invalid")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _describe(
    session: FullRebuildSessionV2,
    capability: CoreCapability,
) -> dict[str, Any]:
    try:
        value = session.describe_capability(capability)
    except AdapterOperationError as exc:
        raise FullRebuildError(exc.reason) from exc
    if not isinstance(value, Mapping):
        raise FullRebuildError("rich_core_authority_invalid")
    try:
        copied = json.loads(canonical_json_bytes(value))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:  # pragma: no cover
        raise FullRebuildError("rich_core_authority_invalid") from exc
    if not isinstance(copied, dict):
        raise FullRebuildError("rich_core_authority_invalid")
    return copied


def _validate_inventory_audit(
    value: Mapping[str, Any],
    *,
    config: FullRebuildConfigV2,
    round_id: str,
    round_kind: str,
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "roundId", "roundKind", "observedAt",
        "guildId", "complete", "truncated", "entryCount", "entrySetSha256",
        "inventoryDigest", "entryBindings", "endpointClasses", "warnings", "errors",
    }
    _exact_keys(value, required)
    expected_bindings = [entry.audit_record() for entry in config.expected_entries]
    if (
        value.get("schemaVersion") != INVENTORY_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("roundId") != round_id
        or value.get("roundKind") != round_kind
        or value.get("guildId") != config.guild_id
        or value.get("complete") is not True
        or value.get("truncated") is not False
        or value.get("entryCount") != config.expected_entry_count
        or value.get("entrySetSha256") != config.expected_entry_set_sha256
        or value.get("entryBindings") != expected_bindings
        or value.get("warnings") != []
        or value.get("errors") != []
        or not HASH_RE.fullmatch(str(value.get("inventoryDigest") or ""))
    ):
        if isinstance(value.get("entryCount"), int) and value.get("entryCount") != config.expected_entry_count:
            raise FullRebuildError("inventory_count_mismatch")
        raise FullRebuildError("inventory_incomplete")
    _timestamp(value["observedAt"])
    endpoint_classes = value.get("endpointClasses")
    if not isinstance(endpoint_classes, dict) or set(endpoint_classes) != set(REQUIRED_INVENTORY_CLASSES):
        raise FullRebuildError("inventory_incomplete")
    for name in REQUIRED_INVENTORY_CLASSES:
        proof = endpoint_classes[name]
        if (
            not isinstance(proof, dict)
            or set(proof) != {"complete", "terminalPage", "errorCount"}
            or proof.get("complete") is not True
            or proof.get("terminalPage") is not True
            or proof.get("errorCount") != 0
        ):
            raise FullRebuildError("inventory_incomplete")
    return deepcopy(dict(value))


def _validate_permission_audit(
    value: Mapping[str, Any],
    *,
    config: FullRebuildConfigV2,
    round_id: str,
    inventory_digest: str,
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "roundId", "guildId", "inventoryDigest",
        "entrySetSha256", "entryCount", "entriesPassed", "applicationIdSha256",
        "messageContentEffective", "runtimeEvidenceOneShot", "permissionErrors",
        "evidenceSha256", "observedAt",
    }
    _exact_keys(value, required)
    if (
        value.get("schemaVersion") != PERMISSION_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("roundId") != round_id
        or value.get("guildId") != config.guild_id
        or value.get("inventoryDigest") != inventory_digest
        or value.get("entrySetSha256") != config.expected_entry_set_sha256
        or value.get("entryCount") != config.expected_entry_count
        or value.get("entriesPassed") != config.expected_entry_count
        or value.get("messageContentEffective") is not True
        or value.get("runtimeEvidenceOneShot") is not True
        or value.get("permissionErrors") != 0
        or not HASH_RE.fullmatch(str(value.get("applicationIdSha256") or ""))
        or not HASH_RE.fullmatch(str(value.get("evidenceSha256") or ""))
    ):
        if value.get("messageContentEffective") is not True:
            raise FullRebuildError("message_content_unavailable")
        raise FullRebuildError("permission_evidence_failed")
    _timestamp(value["observedAt"])
    return deepcopy(dict(value))


def _validate_baseline_audit(
    value: Mapping[str, Any], *, config: FullRebuildConfigV2
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "status", "baselineSha256",
        "archiveManifestSha256", "stateSha256", "queueSha256",
        "verificationSha256", "verifiedAt", "errors",
    }
    _exact_keys(value, required)
    if (
        value.get("schemaVersion") != BASELINE_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("status") != "AUDIT_VERIFIED"
        or value.get("baselineSha256") != config.baseline_sha256
        or value.get("stateSha256") != config.expected_state_sha256
        or value.get("queueSha256") != config.expected_queue_sha256
        or value.get("errors") != []
        or not HASH_RE.fullmatch(str(value.get("archiveManifestSha256") or ""))
        or not HASH_RE.fullmatch(str(value.get("verificationSha256") or ""))
    ):
        raise FullRebuildError("baseline_verification_failed")
    _timestamp(value["verifiedAt"])
    return deepcopy(dict(value))


def _validate_resource_audit(
    value: Mapping[str, Any], *, config: FullRebuildConfigV2
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "status", "assetBudgetIdentitySha256",
        "diskReservationIdentitySha256", "reservedBytes", "allocatedBytes",
        "minimumFreeSpaceBytes", "maxAssetFiles", "maxAssetBytes",
        "consumedAssetFiles", "consumedAssetBytes", "errors",
    }
    _exact_keys(value, required)
    integer_fields = (
        "reservedBytes", "allocatedBytes", "minimumFreeSpaceBytes",
        "maxAssetFiles", "maxAssetBytes", "consumedAssetFiles", "consumedAssetBytes",
    )
    if any(
        not isinstance(value.get(field), int)
        or isinstance(value.get(field), bool)
        or value[field] < 0
        for field in integer_fields
    ):
        raise FullRebuildError("rich_disk_reservation_failed")
    if (
        value.get("schemaVersion") != RESOURCE_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("status") != "RESERVED"
        or value.get("errors") != []
        or not HASH_RE.fullmatch(str(value.get("assetBudgetIdentitySha256") or ""))
        or not HASH_RE.fullmatch(str(value.get("diskReservationIdentitySha256") or ""))
        or value["allocatedBytes"] < value["reservedBytes"]
        or value["minimumFreeSpaceBytes"] != config.limits.minimum_free_space_bytes
        or value["maxAssetFiles"] != config.limits.max_asset_files
        or value["maxAssetBytes"] != config.limits.max_asset_bytes
        or value["consumedAssetFiles"] > value["maxAssetFiles"]
        or value["consumedAssetBytes"] > value["maxAssetBytes"]
    ):
        raise FullRebuildError("rich_disk_reservation_failed")
    return deepcopy(dict(value))


ENTRY_COUNT_FIELDS = (
    "liveIds",
    "canonicalIds",
    "visiblePointers",
    "markdownBlocks",
    "inScopeAssets",
    "verifiedAssets",
    "duplicateCanonicalIds",
    "unknownVisibleFields",
    "attachmentErrors",
    "liveErrors",
    "paginationErrors",
    "newIds",
    "mutableChanges",
    "unresolvedAssetRefreshes",
)


def _validate_sealed_entry_audit(
    value: Mapping[str, Any],
    *,
    config: FullRebuildConfigV2,
    round_id: str,
    round_kind: str,
    entry: EntryBindingV2,
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "roundId", "roundKind", "entryBinding",
        "generationId", "generationSha256", "entryReceiptSha256",
        "liveEvidenceSha256", "cutoff", "trueEmpty", "explicitEmptyProof",
        "terminalPageProof", "runtimeEvidenceConsumed", "perEntryCurrentMutated",
        "counts",
    }
    _exact_keys(value, required)
    counts = value.get("counts")
    if not isinstance(counts, dict) or set(counts) != set(ENTRY_COUNT_FIELDS):
        raise FullRebuildError("rich_full_evidence_failed")
    if any(
        not isinstance(counts.get(field), int)
        or isinstance(counts.get(field), bool)
        or counts[field] < 0
        for field in ENTRY_COUNT_FIELDS
    ):
        raise FullRebuildError("rich_full_evidence_failed")
    cutoff = value.get("cutoff")
    true_empty = value.get("trueEmpty")
    if true_empty is True:
        empty_valid = cutoff is None and value.get("explicitEmptyProof") is True and counts["liveIds"] == 0
    else:
        empty_valid = (
            true_empty is False
            and isinstance(cutoff, str)
            and SNOWFLAKE_RE.fullmatch(cutoff) is not None
            and value.get("explicitEmptyProof") is False
        )
    if (
        value.get("schemaVersion") != SEALED_ENTRY_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("roundId") != round_id
        or value.get("roundKind") != round_kind
        or value.get("entryBinding") != entry.audit_record()
        or not isinstance(value.get("generationId"), str)
        or not value.get("generationId")
        or not HASH_RE.fullmatch(str(value.get("generationSha256") or ""))
        or not HASH_RE.fullmatch(str(value.get("entryReceiptSha256") or ""))
        or not HASH_RE.fullmatch(str(value.get("liveEvidenceSha256") or ""))
        or value.get("terminalPageProof") is not True
        or value.get("runtimeEvidenceConsumed") is not True
        or value.get("perEntryCurrentMutated") is not False
        or not empty_valid
    ):
        if value.get("perEntryCurrentMutated") is not False:
            raise FullRebuildError("rich_entry_current_mutated_early")
        if value.get("terminalPageProof") is not True:
            raise FullRebuildError("rich_pagination_incomplete")
        raise FullRebuildError("rich_full_evidence_failed")
    if (
        counts["liveIds"] != counts["canonicalIds"]
        or counts["canonicalIds"] != counts["visiblePointers"]
        or counts["canonicalIds"] != counts["markdownBlocks"]
        or counts["inScopeAssets"] != counts["verifiedAssets"]
        or any(
            counts[field] != 0
            for field in (
                "duplicateCanonicalIds", "unknownVisibleFields", "attachmentErrors",
                "liveErrors", "paginationErrors", "unresolvedAssetRefreshes",
            )
        )
    ):
        raise FullRebuildError("rich_coverage_incomplete")
    return deepcopy(dict(value))


def _round_counts(entry_audits: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "newIds": sum(int(value["counts"]["newIds"]) for value in entry_audits),
        "mutableChanges": sum(int(value["counts"]["mutableChanges"]) for value in entry_audits),
        "unresolvedAssetRefreshes": sum(
            int(value["counts"]["unresolvedAssetRefreshes"]) for value in entry_audits
        ),
        "liveErrors": sum(int(value["counts"]["liveErrors"]) for value in entry_audits),
        "paginationErrors": sum(int(value["counts"]["paginationErrors"]) for value in entry_audits),
        "unknownVisibleFields": sum(
            int(value["counts"]["unknownVisibleFields"]) for value in entry_audits
        ),
        "attachmentErrors": sum(
            int(value["counts"]["attachmentErrors"]) for value in entry_audits
        ),
    }


def _validate_round_audit(
    value: Mapping[str, Any],
    *,
    config: FullRebuildConfigV2,
    round_id: str,
    round_kind: str,
    inventory_digest: str,
    sealed_hashes: Mapping[str, str],
    entry_audits: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "roundId", "roundKind", "entryCount",
        "entrySetSha256", "inventoryDigest", "sealedEntryAuditSha256ByChannel",
        "counts", "zeroDelta", "explicitTerminalForEveryEntry",
        "runtimeEvidenceFresh", "inventoryStable", "stateQueueInvariant",
        "capabilitiesConsumed", "roundReceiptSha256",
    }
    _exact_keys(value, required)
    counts = _round_counts(entry_audits)
    expected_hashes = dict(sorted(sealed_hashes.items(), key=lambda item: int(item[0])))
    zero = counts["newIds"] == counts["mutableChanges"] == counts["unresolvedAssetRefreshes"] == 0
    if (
        value.get("schemaVersion") != ROUND_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("roundId") != round_id
        or value.get("roundKind") != round_kind
        or value.get("entryCount") != config.expected_entry_count
        or value.get("entrySetSha256") != config.expected_entry_set_sha256
        or value.get("inventoryDigest") != inventory_digest
        or value.get("sealedEntryAuditSha256ByChannel") != expected_hashes
        or value.get("counts") != counts
        or value.get("zeroDelta") is not zero
        or value.get("explicitTerminalForEveryEntry") is not True
        or value.get("runtimeEvidenceFresh") is not True
        or value.get("inventoryStable") is not True
        or value.get("stateQueueInvariant") is not True
        or value.get("capabilitiesConsumed") is not True
        or not HASH_RE.fullmatch(str(value.get("roundReceiptSha256") or ""))
    ):
        if value.get("inventoryStable") is not True:
            raise FullRebuildError("inventory_drift")
        if value.get("explicitTerminalForEveryEntry") is not True:
            raise FullRebuildError("rich_pagination_incomplete")
        raise FullRebuildError("rich_full_run_incomplete")
    return deepcopy(dict(value))


def _validate_ready_audit(
    value: Mapping[str, Any],
    *,
    config: FullRebuildConfigV2,
    round_hashes: Sequence[str],
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "status", "entryCount", "entrySetSha256",
        "roundAuditSha256s", "zeroRoundStreak", "stateSha256", "queueSha256",
        "coverage", "liveErrors", "paginationErrors", "unknownVisibleFields",
        "attachmentErrors", "runtimeEvidenceConsumed", "runManifestSha256",
        "fullRunBindingSha256",
    }
    _exact_keys(value, required)
    coverage = value.get("coverage")
    if not isinstance(coverage, dict) or set(coverage) != {
        "id", "visible", "markdown", "binary"
    }:
        raise FullRebuildError("rich_coverage_incomplete")
    for dimension in coverage.values():
        if (
            not isinstance(dimension, dict)
            or set(dimension) != {"expected", "verified"}
            or not isinstance(dimension.get("expected"), int)
            or isinstance(dimension.get("expected"), bool)
            or dimension.get("expected") < 0
            or dimension.get("verified") != dimension.get("expected")
        ):
            raise FullRebuildError("rich_coverage_incomplete")
    if (
        value.get("schemaVersion") != RUN_READY_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("status") != "RUNTIME_READY"
        or value.get("entryCount") != config.expected_entry_count
        or value.get("entrySetSha256") != config.expected_entry_set_sha256
        or value.get("roundAuditSha256s") != list(round_hashes)
        or value.get("zeroRoundStreak") != 2
        or value.get("stateSha256") != config.expected_state_sha256
        or value.get("queueSha256") != config.expected_queue_sha256
        or any(value.get(field) != 0 for field in (
            "liveErrors", "paginationErrors", "unknownVisibleFields", "attachmentErrors"
        ))
        or value.get("runtimeEvidenceConsumed") is not True
        or not HASH_RE.fullmatch(str(value.get("runManifestSha256") or ""))
        or not HASH_RE.fullmatch(str(value.get("fullRunBindingSha256") or ""))
    ):
        raise FullRebuildError("rich_full_run_incomplete")
    return deepcopy(dict(value))


def _validate_root_current_audit(
    value: Mapping[str, Any], *, config: FullRebuildConfigV2
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "published", "runManifestSha256",
        "fullRunBindingSha256", "priorPointerSha256", "newPointerSha256",
        "directoryFsync", "rollbackPrepared",
    }
    _exact_keys(value, required)
    prior = value.get("priorPointerSha256")
    if prior is not None and not HASH_RE.fullmatch(str(prior)):
        raise FullRebuildError("rich_root_publish_failed")
    if (
        value.get("schemaVersion") != ROOT_CURRENT_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("published") is not True
        or value.get("directoryFsync") is not True
        or value.get("rollbackPrepared") is not True
        or not HASH_RE.fullmatch(str(value.get("runManifestSha256") or ""))
        or not HASH_RE.fullmatch(str(value.get("fullRunBindingSha256") or ""))
        or not HASH_RE.fullmatch(str(value.get("newPointerSha256") or ""))
    ):
        raise FullRebuildError("rich_root_publish_failed")
    return deepcopy(dict(value))


def _validate_root_grant_audit(
    value: Mapping[str, Any],
    *,
    config: FullRebuildConfigV2,
    root_current: Mapping[str, Any],
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "readback", "runManifestSha256",
        "fullRunBindingSha256", "newPointerSha256", "selectedEntryCount",
        "entrySetSha256", "readerIndexerCanary", "rollbackVerified",
    }
    _exact_keys(value, required)
    if (
        value.get("schemaVersion") != ROOT_GRANT_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("readback") is not True
        or value.get("runManifestSha256") != root_current["runManifestSha256"]
        or value.get("fullRunBindingSha256") != root_current["fullRunBindingSha256"]
        or value.get("newPointerSha256") != root_current["newPointerSha256"]
        or value.get("selectedEntryCount") != config.expected_entry_count
        or value.get("entrySetSha256") != config.expected_entry_set_sha256
        or value.get("readerIndexerCanary") is not True
        or value.get("rollbackVerified") is not True
    ):
        raise FullRebuildError("rich_root_readback_failed")
    return deepcopy(dict(value))


def _validate_compatibility_audit(
    value: Mapping[str, Any], *, config: FullRebuildConfigV2
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "status", "entryCount", "updatedEntries",
        "verifiedEntries", "authoritativeRootUnchanged", "errors",
    }
    _exact_keys(value, required)
    if (
        value.get("schemaVersion") != COMPATIBILITY_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("status") != "AUDIT_ONLY"
        or value.get("entryCount") != config.expected_entry_count
        or value.get("updatedEntries") != config.expected_entry_count
        or value.get("verifiedEntries") != config.expected_entry_count
        or value.get("authoritativeRootUnchanged") is not True
        or value.get("errors") != []
    ):
        raise FullRebuildError("rich_compatibility_publish_failed")
    return deepcopy(dict(value))


def _validate_committed_inspection_audit(
    value: Mapping[str, Any],
    *,
    config: FullRebuildConfigV2,
    root: Mapping[str, Any],
    final_receipt_sha256: str,
) -> dict[str, Any]:
    required = {
        "schemaVersion", "runId", "readOnly", "runManifestSha256",
        "fullRunBindingSha256", "priorPointerSha256", "newPointerSha256",
        "selectedEntryCount", "entrySetSha256", "readerIndexerCanary",
        "finalReceiptSha256", "stateQueueInvariant",
    }
    _exact_keys(value, required)
    if (
        value.get("schemaVersion") != COMMITTED_READBACK_AUDIT_SCHEMA
        or value.get("runId") != config.run_id
        or value.get("readOnly") is not True
        or value.get("runManifestSha256") != root.get("runManifestSha256")
        or value.get("fullRunBindingSha256") != root.get("fullRunBindingSha256")
        or value.get("priorPointerSha256") != root.get("priorPointerSha256")
        or value.get("newPointerSha256") != root.get("newPointerSha256")
        or value.get("selectedEntryCount") != config.expected_entry_count
        or value.get("entrySetSha256") != config.expected_entry_set_sha256
        or value.get("readerIndexerCanary") is not True
        or value.get("finalReceiptSha256") != final_receipt_sha256
        or value.get("stateQueueInvariant") is not True
    ):
        raise FullRebuildError("committed_readback_failed")
    return deepcopy(dict(value))


def _call_adapter(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return operation(*args, **kwargs)
    except AdapterOperationError as exc:
        raise FullRebuildError(exc.reason) from exc


def _set_phase(journal: dict[str, Any], phase: str) -> None:
    current = journal.get("phase")
    if phase not in PHASES:
        raise FullRebuildError("journal_corrupt")
    if current in TERMINAL_PHASES:
        current = journal.get("resumePhase")
    if not isinstance(current, str) or current not in PHASES:
        raise FullRebuildError("journal_corrupt")
    if _phase_rank(phase) < _phase_rank(current):
        return
    journal["phase"] = phase
    journal["resumePhase"] = None


def _binding_for_journal(config: FullRebuildConfigV2) -> dict[str, Any]:
    return {
        "runId": config.run_id,
        "archiveRootIdentitySha256": json_sha256(str(config.archive_root)),
        "guildId": config.guild_id,
        "expectedEntryCount": config.expected_entry_count,
        "expectedEntrySetSha256": config.expected_entry_set_sha256,
        "baselineSha256": config.baseline_sha256,
        "stateSha256": config.expected_state_sha256,
        "queueSha256": config.expected_queue_sha256,
        "adapterCodeSha256": config.adapter_code_sha256,
        "richCoreCodeSha256": config.rich_core_code_sha256,
        "coordinatorCodeSha256": config.coordinator_code_sha256,
        "configurationSha256": config.configuration_sha256,
        "expectedEntriesArtifactSha256": config.expected_entries_artifact_sha256,
        "runtimeManifestSha256": config.runtime_manifest_sha256,
        "limits": dict(config.limits.as_tuple()),
    }


def _validate_journal_binding(
    journal: Mapping[str, Any], config: FullRebuildConfigV2
) -> None:
    binding = _binding_for_journal(config)
    if (
        journal.get("schemaVersion") != JOURNAL_SCHEMA
        or journal.get("coordinatorVersion") != COORDINATOR_SCHEMA
        or any(journal.get(key) != value for key, value in binding.items())
        or journal.get("phase") not in {*PHASES, *TERMINAL_PHASES}
        or not isinstance(journal.get("rounds"), list)
        or not isinstance(journal.get("zeroRoundStreak"), int)
        or journal.get("zeroRoundStreak") not in {0, 1, 2}
        or journal.get("activeRound") is not None
        and not isinstance(journal.get("activeRound"), dict)
        or not isinstance(journal.get("root"), dict)
    ):
        raise FullRebuildError("journal_binding_mismatch")
    if journal.get("phase") in TERMINAL_PHASES and journal.get("resumePhase") not in PHASES:
        raise FullRebuildError("journal_corrupt")
    seen_rounds: list[str] = []
    for index, record in enumerate(journal["rounds"], start=1):
        if not isinstance(record, dict):
            raise FullRebuildError("journal_corrupt")
        kind = record.get("roundKind")
        round_id = record.get("roundId")
        if (
            kind not in ROUND_KINDS
            or round_id != f"round-{index:04d}-{kind}"
            or not HASH_RE.fullmatch(str(record.get("roundAuditSha256") or ""))
            or not isinstance(record.get("entries"), dict)
            or len(record["entries"]) != config.expected_entry_count
            or set(record["entries"]) != {entry.channel_id for entry in config.expected_entries}
            or not isinstance(record.get("audit"), dict)
            or json_sha256(record["audit"]) != record["roundAuditSha256"]
        ):
            raise FullRebuildError("journal_corrupt")
        seen_rounds.append(kind)
    if seen_rounds:
        if seen_rounds[0] != "baseline" or len(seen_rounds) > 1 and seen_rounds[1] != "delta":
            raise FullRebuildError("journal_corrupt")
        if any(kind != "zero" for kind in seen_rounds[2:]):
            raise FullRebuildError("journal_corrupt")
    active = journal.get("activeRound")
    if active is not None:
        expected_index = len(journal["rounds"]) + 1
        if (
            active.get("roundKind") not in ROUND_KINDS
            or active.get("roundId")
            != f"round-{expected_index:04d}-{active.get('roundKind')}"
            or not isinstance(active.get("entries"), dict)
            or not isinstance(active.get("inventoryAudit"), dict)
            or not isinstance(active.get("permissionAudit"), dict)
        ):
            raise FullRebuildError("journal_corrupt")


def _next_round_kind(journal: Mapping[str, Any]) -> str | None:
    active = journal.get("activeRound")
    if isinstance(active, dict):
        return str(active["roundKind"])
    kinds = [record["roundKind"] for record in journal["rounds"]]
    if not kinds:
        return "baseline"
    if len(kinds) == 1:
        return "delta"
    if int(journal.get("zeroRoundStreak") or 0) < 2:
        return "zero"
    return None


class FullRichRebuildCoordinatorV2:
    def __init__(
        self,
        config: FullRebuildConfigV2,
        *,
        adapter: ManagedRichCoreAdapterV3,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.config = validate_config(config)
        self.adapter = require_adapter(adapter)
        self.fault = fault_injector or (lambda _point: None)
        self.tracker = CapabilityTracker()
        self.ledger = DurableJournal(
            archive_root=self.config.archive_root,
            run_id=self.config.run_id,
            fault_injector=self.fault,
        )

    def _state_queue_bytes(self) -> tuple[bytes, bytes]:
        state = _safe_read_bytes(self.config.state_path)
        queue = _safe_read_bytes(self.config.queue_path)
        state_sha = bytes_sha256(state)
        queue_sha = bytes_sha256(queue)
        if state_sha != self.config.expected_state_sha256:
            raise FullRebuildError("state_hash_mismatch")
        if queue_sha != self.config.expected_queue_sha256:
            raise FullRebuildError("queue_hash_mismatch")
        return state, queue

    def _load_or_create_journal(self) -> dict[str, Any]:
        if self.ledger.exists():
            journal = self.ledger.load()
            _validate_journal_binding(journal, self.config)
            if journal["phase"] in TERMINAL_PHASES:
                old_phase = journal["phase"]
                resume_phase = journal["resumePhase"]
                journal = self.ledger.record(
                    journal,
                    event="run_resumed",
                    payload={"from": old_phase, "resumePhase": resume_phase},
                    update=lambda changed: (
                        changed.update({
                            "phase": resume_phase,
                            "resumePhase": None,
                            "lastError": None,
                        })
                    ),
                )
            return journal
        self.ledger.ensure_layout()
        return self.ledger.create(_binding_for_journal(self.config))

    def _record_failure(
        self,
        session: FullRebuildSessionV2,
        journal: Mapping[str, Any] | None,
        reason: str,
        *,
        paused: bool = False,
    ) -> None:
        if journal is None or not self.ledger.exists():
            return
        try:
            _assert_canonical_lock(session)
            current_phase = journal.get("phase")
            if current_phase in TERMINAL_PHASES:
                current_phase = journal.get("resumePhase")
            if current_phase not in PHASES:
                return
            if current_phase == "COMMITTED":
                # A committed rerun is a strictly read-only verification path.
                return
            self.ledger.record(
                journal,
                event="run_paused" if paused else "run_failed",
                payload={"reason": reason, "safePhase": current_phase},
                update=lambda changed: changed.update({
                    "phase": "PAUSED" if paused else "FAILED",
                    "resumePhase": current_phase,
                    "lastError": reason,
                }),
            )
        except (OSError, FullRebuildError):
            # Preserve the original failure.  A missing terminal receipt never
            # makes the run successful and will be visible on next verification.
            return

    def _baseline(
        self, session: FullRebuildSessionV2
    ) -> tuple[BaselineCapabilityV2, dict[str, Any]]:
        capability = _call_adapter(
            session.verify_immutable_baseline,
            BaselineVerificationRequestV2(
                run_id=self.config.run_id,
                baseline_dir=self.config.baseline_dir,
                expected_baseline_sha256=self.config.baseline_sha256,
                expected_state_sha256=self.config.expected_state_sha256,
                expected_queue_sha256=self.config.expected_queue_sha256,
            ),
        )
        baseline = self.tracker.accept(capability, "baseline")
        assert isinstance(baseline, BaselineCapabilityV2)
        audit = _validate_baseline_audit(_describe(session, baseline), config=self.config)
        return baseline, audit

    def _inventory_and_permissions(
        self,
        session: FullRebuildSessionV2,
        *,
        round_id: str,
        round_kind: str,
    ) -> tuple[
        InventoryCapabilityV3,
        PermissionCapabilityV2,
        dict[str, Any],
        dict[str, Any],
    ]:
        inventory_value = _call_adapter(
            session.collect_fresh_inventory,
            InventoryRequestV3(
                run_id=self.config.run_id,
                round_id=round_id,
                round_kind=round_kind,
                guild_id=self.config.guild_id,
                expected_entry_count=self.config.expected_entry_count,
                expected_entry_set_sha256=self.config.expected_entry_set_sha256,
                expected_entries=self.config.expected_entries,
            ),
        )
        inventory = self.tracker.accept(inventory_value, "inventory")
        assert isinstance(inventory, InventoryCapabilityV3)
        inventory_audit = _validate_inventory_audit(
            _describe(session, inventory),
            config=self.config,
            round_id=round_id,
            round_kind=round_kind,
        )
        permission_value = _call_adapter(
            session.prove_permissions,
            PermissionRequestV2(
                run_id=self.config.run_id,
                round_id=round_id,
                guild_id=self.config.guild_id,
                expected_entry_count=self.config.expected_entry_count,
                expected_entry_set_sha256=self.config.expected_entry_set_sha256,
            ),
            inventory=inventory,
        )
        permissions = self.tracker.accept(permission_value, "permissions")
        assert isinstance(permissions, PermissionCapabilityV2)
        permission_audit = _validate_permission_audit(
            _describe(session, permissions),
            config=self.config,
            round_id=round_id,
            inventory_digest=inventory_audit["inventoryDigest"],
        )
        return inventory, permissions, inventory_audit, permission_audit

    def _resources(
        self,
        session: FullRebuildSessionV2,
        *,
        baseline: BaselineCapabilityV2,
        inventory: InventoryCapabilityV3,
        journal: Mapping[str, Any],
    ) -> tuple[RunResourcesCapabilityV2, dict[str, Any]]:
        value = _call_adapter(
            session.acquire_run_resources,
            ResourceReservationRequestV2(
                run_id=self.config.run_id,
                archive_root=self.config.archive_root,
                max_asset_files=self.config.limits.max_asset_files,
                max_asset_bytes=self.config.limits.max_asset_bytes,
                minimum_free_space_bytes=self.config.limits.minimum_free_space_bytes,
                resume_audit_sha256=journal.get("resourceAuditSha256"),
            ),
            baseline=baseline,
            inventory=inventory,
        )
        resources = self.tracker.accept(value, "resources", reusable=True)
        assert isinstance(resources, RunResourcesCapabilityV2)
        audit = _validate_resource_audit(_describe(session, resources), config=self.config)
        return resources, audit

    def _recover_rounds(
        self,
        session: FullRebuildSessionV2,
        *,
        resources: RunResourcesCapabilityV2,
        journal: Mapping[str, Any],
    ) -> list[FullRoundCapabilityV2]:
        capabilities: list[FullRoundCapabilityV2] = []
        for record in journal["rounds"]:
            value = _call_adapter(
                session.recover_sealed_round,
                ResumeRoundRequestV2(
                    run_id=self.config.run_id,
                    round_id=record["roundId"],
                    round_kind=record["roundKind"],
                    round_audit_sha256=record["roundAuditSha256"],
                ),
                resources=resources,
            )
            capability = self.tracker.accept(value, "round")
            assert isinstance(capability, FullRoundCapabilityV2)
            audit = _describe(session, capability)
            if audit != record["audit"] or json_sha256(audit) != record["roundAuditSha256"]:
                raise FullRebuildError("journal_binding_mismatch")
            capabilities.append(capability)
        return capabilities

    def _final_payload(
        self,
        *,
        journal: Mapping[str, Any],
        root_summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        round_hashes = [record["roundAuditSha256"] for record in journal["rounds"]]
        return {
            "runId": self.config.run_id,
            "phase": "COMMITTED",
            "entryCount": self.config.expected_entry_count,
            "entrySetSha256": self.config.expected_entry_set_sha256,
            "stateBeforeSha256": self.config.expected_state_sha256,
            "stateAfterSha256": self.config.expected_state_sha256,
            "queueBeforeSha256": self.config.expected_queue_sha256,
            "queueAfterSha256": self.config.expected_queue_sha256,
            "roundAuditSha256s": round_hashes,
            "zeroRoundStreak": 2,
            **deepcopy(dict(root_summary)),
        }

    def _verify_committed(
        self,
        session: FullRebuildSessionV2,
        *,
        journal: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Read-only idempotent verification for an already committed run."""

        _assert_canonical_lock(session)
        root = journal.get("root")
        required_root = {
            "readyAuditSha256", "publishedAuditSha256", "grantAuditSha256",
            "compatibilityAuditSha256", "runManifestSha256",
            "fullRunBindingSha256", "priorPointerSha256", "newPointerSha256",
        }
        if (
            not isinstance(root, dict)
            or set(root) != required_root
            or journal.get("zeroRoundStreak") != 2
            or len(journal.get("rounds", [])) < 4
        ):
            raise FullRebuildError("committed_readback_failed")
        for key in required_root - {"priorPointerSha256"}:
            _require_hash(root.get(key), "committed_readback_failed")
        prior = root.get("priorPointerSha256")
        if prior is not None:
            _require_hash(prior, "committed_readback_failed")
        expected_payload = self._final_payload(journal=journal, root_summary=root)
        expected_receipt = {
            "schemaVersion": FINAL_RECEIPT_SCHEMA,
            "status": "AUDIT_ONLY",
            **expected_payload,
        }
        actual_receipt, final_receipt_sha256 = self.ledger.load_final_receipt()
        if actual_receipt != expected_receipt:
            raise FullRebuildError("committed_readback_failed")
        request = CommittedRunInspectionRequestV2(
            run_id=self.config.run_id,
            expected_entry_count=self.config.expected_entry_count,
            expected_entry_set_sha256=self.config.expected_entry_set_sha256,
            run_manifest_sha256=root["runManifestSha256"],
            full_run_binding_sha256=root["fullRunBindingSha256"],
            prior_pointer_sha256=prior,
            new_pointer_sha256=root["newPointerSha256"],
            final_receipt_sha256=final_receipt_sha256,
        )
        value = _call_adapter(session.inspect_committed_full_rebuild, request)
        inspection = self.tracker.accept(value, "committed_inspection")
        assert isinstance(inspection, CommittedRunInspectionCapabilityV2)
        _validate_committed_inspection_audit(
            _describe(session, inspection),
            config=self.config,
            root=root,
            final_receipt_sha256=final_receipt_sha256,
        )
        _assert_state_queue(
            self.config,
            state_sha256=self.config.expected_state_sha256,
            queue_sha256=self.config.expected_queue_sha256,
        )
        return {
            "ok": True,
            "status": "committed",
            "idempotentReadback": True,
            "runId": self.config.run_id,
            "entryCount": self.config.expected_entry_count,
            "zeroRounds": 2,
            "runManifestSha256": root["runManifestSha256"],
            "fullRunBindingSha256": root["fullRunBindingSha256"],
        }

    def _execute_round(
        self,
        session: FullRebuildSessionV2,
        *,
        journal: dict[str, Any],
        resources: RunResourcesCapabilityV2,
        prior_round: FullRoundCapabilityV2 | None,
        inventory_bundle: tuple[
            InventoryCapabilityV3,
            PermissionCapabilityV2,
            dict[str, Any],
            dict[str, Any],
        ] | None = None,
    ) -> tuple[dict[str, Any], FullRoundCapabilityV2]:
        round_kind = _next_round_kind(journal)
        if round_kind is None:
            raise FullRebuildError("rich_full_run_incomplete")
        sequence = len(journal["rounds"]) + 1
        round_id = f"round-{sequence:04d}-{round_kind}"
        if inventory_bundle is None:
            inventory_bundle = self._inventory_and_permissions(
                session, round_id=round_id, round_kind=round_kind
            )
        inventory, permissions, inventory_audit, permission_audit = inventory_bundle
        _assert_state_queue(
            self.config,
            state_sha256=self.config.expected_state_sha256,
            queue_sha256=self.config.expected_queue_sha256,
        )
        active = journal.get("activeRound")
        if active is None:
            journal = self.ledger.record(
                journal,
                event="round_started",
                payload={
                    "roundId": round_id,
                    "roundKind": round_kind,
                    "inventoryAuditSha256": json_sha256(inventory_audit),
                    "permissionAuditSha256": json_sha256(permission_audit),
                },
                update=lambda changed: changed.update({
                    "activeRound": {
                        "roundId": round_id,
                        "roundKind": round_kind,
                        "inventoryAudit": inventory_audit,
                        "permissionAudit": permission_audit,
                        "entries": {},
                    }
                }),
            )
            active = journal["activeRound"]
        elif (
            active.get("roundId") != round_id
            or active.get("roundKind") != round_kind
            or active.get("inventoryAudit", {}).get("entrySetSha256")
            != inventory_audit["entrySetSha256"]
        ):
            raise FullRebuildError("inventory_drift")

        sealed_capabilities: list[SealedFullEntryCapabilityV2] = []
        sealed_audits: list[dict[str, Any]] = []
        sealed_hashes: dict[str, str] = {}
        for entry_index, entry in enumerate(self.config.expected_entries, start=1):
            existing = journal["activeRound"]["entries"].get(entry.channel_id)
            if existing is not None:
                value = _call_adapter(
                    session.recover_sealed_entry,
                    ResumeSealedEntryRequestV2(
                        run_id=self.config.run_id,
                        round_id=round_id,
                        round_kind=round_kind,
                        entry=entry,
                        sealed_audit_sha256=existing["sealedAuditSha256"],
                    ),
                    prior_round=prior_round,
                    inventory=inventory,
                    permissions=permissions,
                    resources=resources,
                )
                sealed = self.tracker.accept(value, "sealed_entry")
                assert isinstance(sealed, SealedFullEntryCapabilityV2)
                audit = _validate_sealed_entry_audit(
                    _describe(session, sealed),
                    config=self.config,
                    round_id=round_id,
                    round_kind=round_kind,
                    entry=entry,
                )
                if audit != existing["audit"] or json_sha256(audit) != existing["sealedAuditSha256"]:
                    raise FullRebuildError("journal_binding_mismatch")
            else:
                request = EntryStageRequestV3(
                    run_id=self.config.run_id,
                    round_id=round_id,
                    round_kind=round_kind,
                    entry=entry,
                    sequence=entry_index,
                )
                self.fault(f"before_stage:{round_id}:{entry.channel_id}")
                stage_value = _call_adapter(
                    session.collect_and_stage_full_snapshot,
                    request,
                    prior_round=prior_round,
                    inventory=inventory,
                    permissions=permissions,
                    resources=resources,
                )
                stage = self.tracker.accept(stage_value, "stage")
                assert isinstance(stage, FullStageCapabilityV2)
                _assert_state_queue(
                    self.config,
                    state_sha256=self.config.expected_state_sha256,
                    queue_sha256=self.config.expected_queue_sha256,
                )
                reserved_value = _call_adapter(
                    session.reserve_full_stage_assets,
                    stage,
                    resources=resources,
                )
                reserved = self.tracker.accept(reserved_value, "reserved_stage")
                assert isinstance(reserved, ReservedFullStageCapabilityV2)
                _assert_state_queue(
                    self.config,
                    state_sha256=self.config.expected_state_sha256,
                    queue_sha256=self.config.expected_queue_sha256,
                )
                prepared_value = _call_adapter(
                    session.install_full_pass_evidence,
                    reserved,
                    inventory=inventory,
                    permissions=permissions,
                )
                prepared = self.tracker.accept(prepared_value, "prepared_stage")
                assert isinstance(prepared, PreparedFullStageCapabilityV2)
                _assert_state_queue(
                    self.config,
                    state_sha256=self.config.expected_state_sha256,
                    queue_sha256=self.config.expected_queue_sha256,
                )
                sealed_value = _call_adapter(session.seal_full_entry, prepared)
                sealed = self.tracker.accept(sealed_value, "sealed_entry")
                assert isinstance(sealed, SealedFullEntryCapabilityV2)
                audit = _validate_sealed_entry_audit(
                    _describe(session, sealed),
                    config=self.config,
                    round_id=round_id,
                    round_kind=round_kind,
                    entry=entry,
                )
                _assert_state_queue(
                    self.config,
                    state_sha256=self.config.expected_state_sha256,
                    queue_sha256=self.config.expected_queue_sha256,
                )
                sealed_hash = json_sha256(audit)
                self.fault(f"after_seal_before_journal:{round_id}:{entry.channel_id}")
                journal = self.ledger.record(
                    journal,
                    event="entry_sealed",
                    payload={
                        "roundId": round_id,
                        "roundKind": round_kind,
                        "channelId": entry.channel_id,
                        "sealedAuditSha256": sealed_hash,
                    },
                    update=lambda changed, channel_id=entry.channel_id, audit=audit, sealed_hash=sealed_hash: (
                        changed["activeRound"]["entries"].update({
                            channel_id: {
                                "sealedAuditSha256": sealed_hash,
                                "audit": audit,
                            }
                        })
                    ),
                )
            audit_hash = json_sha256(audit)
            sealed_capabilities.append(sealed)
            sealed_audits.append(audit)
            sealed_hashes[entry.channel_id] = audit_hash

        request = RoundSealRequestV2(
            run_id=self.config.run_id,
            round_id=round_id,
            round_kind=round_kind,
            sequence=sequence,
            expected_entry_count=self.config.expected_entry_count,
            expected_entry_set_sha256=self.config.expected_entry_set_sha256,
            sealed_audit_sha256_by_channel=tuple(
                sorted(sealed_hashes.items(), key=lambda item: int(item[0]))
            ),
        )
        self.fault(f"before_round_seal:{round_id}")
        round_value = _call_adapter(
            session.seal_round,
            request,
            inventory=inventory,
            permissions=permissions,
            sealed_entries=tuple(sealed_capabilities),
        )
        round_capability = self.tracker.accept(round_value, "round")
        assert isinstance(round_capability, FullRoundCapabilityV2)
        round_audit = _validate_round_audit(
            _describe(session, round_capability),
            config=self.config,
            round_id=round_id,
            round_kind=round_kind,
            inventory_digest=inventory_audit["inventoryDigest"],
            sealed_hashes=sealed_hashes,
            entry_audits=sealed_audits,
        )
        round_hash = json_sha256(round_audit)
        counts = _round_counts(sealed_audits)
        zero_delta = (
            counts["newIds"] == 0
            and counts["mutableChanges"] == 0
            and counts["unresolvedAssetRefreshes"] == 0
        )

        def finish_round(changed: dict[str, Any]) -> None:
            record = {
                "roundId": round_id,
                "roundKind": round_kind,
                "roundAuditSha256": round_hash,
                "audit": round_audit,
                "entries": deepcopy(changed["activeRound"]["entries"]),
            }
            changed["rounds"].append(record)
            changed["activeRound"] = None
            if round_kind == "zero":
                changed["zeroRoundStreak"] = (
                    min(2, int(changed["zeroRoundStreak"]) + 1)
                    if zero_delta
                    else 0
                )
            else:
                changed["zeroRoundStreak"] = 0
            if round_kind == "baseline":
                _set_phase(changed, "BASELINE_COMPLETE")
            elif round_kind == "delta":
                _set_phase(changed, "DELTA_CONVERGING")
            elif changed["zeroRoundStreak"] >= 2:
                _set_phase(changed, "VERIFYING")
            else:
                _set_phase(changed, "DELTA_CONVERGING")

        journal = self.ledger.record(
            journal,
            event="round_sealed",
            payload={
                "roundId": round_id,
                "roundKind": round_kind,
                "roundAuditSha256": round_hash,
                "zeroDelta": zero_delta,
            },
            update=finish_round,
        )
        return journal, round_capability

    def _run_locked(self, session: FullRebuildSessionV2) -> dict[str, Any]:
        _verify_runtime_authority_under_lock(self.config, session=session)
        self._state_queue_bytes()
        journal = self._load_or_create_journal()
        if journal.get("phase") == "COMMITTED":
            return self._verify_committed(session, journal=journal)
        baseline, baseline_audit = self._baseline(session)
        baseline_hash = json_sha256(baseline_audit)
        journal = self.ledger.record(
            journal,
            event="baseline_verified",
            payload={"baselineAuditSha256": baseline_hash},
            update=lambda changed: changed.update({
                "baselineAuditSha256": baseline_hash,
            }),
        )

        next_kind = _next_round_kind(journal)
        pending_round_id = (
            journal["activeRound"]["roundId"]
            if isinstance(journal.get("activeRound"), dict)
            else (
                f"round-{len(journal['rounds']) + 1:04d}-{next_kind}"
                if next_kind is not None
                else "final-verification"
            )
        )
        inventory_kind = next_kind or "zero"
        inventory_bundle = self._inventory_and_permissions(
            session,
            round_id=pending_round_id,
            round_kind=inventory_kind,
        )
        inventory, _permissions, inventory_audit, permission_audit = inventory_bundle
        if _phase_rank(journal["phase"]) < _phase_rank("INVENTORY_VERIFIED"):
            journal = self.ledger.record(
                journal,
                event="inventory_verified",
                payload={
                    "inventoryAuditSha256": json_sha256(inventory_audit),
                    "permissionAuditSha256": json_sha256(permission_audit),
                },
                update=lambda changed: _set_phase(changed, "INVENTORY_VERIFIED"),
            )
        resources, resource_audit = self._resources(
            session,
            baseline=baseline,
            inventory=inventory,
            journal=journal,
        )
        resource_hash = json_sha256(resource_audit)

        def resources_ready(changed: dict[str, Any]) -> None:
            changed["resourceAuditSha256"] = resource_hash
            _set_phase(changed, "BASELINE_FROZEN")
            _set_phase(changed, "REBUILDING")

        journal = self.ledger.record(
            journal,
            event="resources_reserved",
            payload={"resourceAuditSha256": resource_hash},
            update=resources_ready,
        )
        _assert_state_queue(
            self.config,
            state_sha256=self.config.expected_state_sha256,
            queue_sha256=self.config.expected_queue_sha256,
        )
        round_capabilities = self._recover_rounds(
            session, resources=resources, journal=journal
        )
        first_inventory_bundle = inventory_bundle
        while _next_round_kind(journal) is not None:
            next_kind = _next_round_kind(journal)
            assert next_kind is not None
            zero_attempts = sum(
                1 for record in journal["rounds"] if record["roundKind"] == "zero"
            )
            if next_kind == "zero" and zero_attempts >= self.config.limits.max_convergence_rounds:
                raise FullRebuildError("rich_convergence_exhausted")
            prior = round_capabilities[-1] if round_capabilities else None
            journal, round_capability = self._execute_round(
                session,
                journal=journal,
                resources=resources,
                prior_round=prior,
                inventory_bundle=first_inventory_bundle,
            )
            first_inventory_bundle = None
            round_capabilities.append(round_capability)

        _assert_state_queue(
            self.config,
            state_sha256=self.config.expected_state_sha256,
            queue_sha256=self.config.expected_queue_sha256,
        )
        round_hashes = tuple(
            record["roundAuditSha256"] for record in journal["rounds"]
        )
        if (
            len(round_capabilities) != len(journal["rounds"])
            or len(journal["rounds"]) < 4
            or journal["rounds"][0]["roundKind"] != "baseline"
            or journal["rounds"][1]["roundKind"] != "delta"
            or journal["rounds"][-2]["roundKind"] != "zero"
            or journal["rounds"][-1]["roundKind"] != "zero"
            or journal["zeroRoundStreak"] != 2
        ):
            raise FullRebuildError("rich_full_run_incomplete")
        ready_value = _call_adapter(
            session.finalize_full_rebuild,
            FinalizeFullRebuildRequestV2(
                run_id=self.config.run_id,
                expected_entry_count=self.config.expected_entry_count,
                expected_entry_set_sha256=self.config.expected_entry_set_sha256,
                state_sha256=self.config.expected_state_sha256,
                queue_sha256=self.config.expected_queue_sha256,
                round_audit_sha256s=round_hashes,
                zero_round_streak=2,
            ),
            rounds=tuple(round_capabilities),
            resources=resources,
        )
        ready = self.tracker.accept(ready_value, "ready")
        assert isinstance(ready, FullRunReadyCapabilityV2)
        ready_audit = _validate_ready_audit(
            _describe(session, ready),
            config=self.config,
            round_hashes=round_hashes,
        )
        ready_hash = json_sha256(ready_audit)
        journal = self.ledger.record(
            journal,
            event="run_ready",
            payload={"readyAuditSha256": ready_hash},
            update=lambda changed: (
                changed["root"].update({"readyAuditSha256": ready_hash}),
                _set_phase(changed, "READY_TO_COMMIT"),
            ),
        )
        _assert_state_queue(
            self.config,
            state_sha256=self.config.expected_state_sha256,
            queue_sha256=self.config.expected_queue_sha256,
        )
        root_request = RootPublishRequestV2(
            run_id=self.config.run_id,
            archive_root=self.config.archive_root,
            expected_entry_count=self.config.expected_entry_count,
            expected_entry_set_sha256=self.config.expected_entry_set_sha256,
            state_sha256=self.config.expected_state_sha256,
            queue_sha256=self.config.expected_queue_sha256,
        )
        self.fault("before_root_publish")
        published_value = _call_adapter(
            session.publish_root_run_current, root_request, ready=ready
        )
        published = self.tracker.accept(published_value, "root_current")
        assert isinstance(published, RootCurrentCapabilityV2)
        published_audit = _validate_root_current_audit(
            _describe(session, published), config=self.config
        )
        if (
            published_audit["runManifestSha256"] != ready_audit["runManifestSha256"]
            or published_audit["fullRunBindingSha256"]
            != ready_audit["fullRunBindingSha256"]
        ):
            raise FullRebuildError("rich_root_publish_failed")
        self.fault("after_root_publish_before_readback")
        grant_value = _call_adapter(
            session.inspect_root_run_current,
            root_request,
            ready=ready,
            published=published,
        )
        grant = self.tracker.accept(grant_value, "root_grant")
        assert isinstance(grant, RootCommitGrantV2)
        grant_audit = _validate_root_grant_audit(
            _describe(session, grant),
            config=self.config,
            root_current=published_audit,
        )
        self.fault("after_root_readback_before_compatibility")
        _assert_state_queue(
            self.config,
            state_sha256=self.config.expected_state_sha256,
            queue_sha256=self.config.expected_queue_sha256,
        )
        compatibility_value = _call_adapter(
            session.publish_compatibility_currents,
            CompatibilityPublishRequestV2(
                run_id=self.config.run_id,
                expected_entry_count=self.config.expected_entry_count,
                expected_entry_set_sha256=self.config.expected_entry_set_sha256,
            ),
            grant=grant,
        )
        if not isinstance(compatibility_value, Mapping):
            raise FullRebuildError("rich_compatibility_publish_failed")
        compatibility_audit = _validate_compatibility_audit(
            compatibility_value, config=self.config
        )
        _assert_state_queue(
            self.config,
            state_sha256=self.config.expected_state_sha256,
            queue_sha256=self.config.expected_queue_sha256,
        )

        root_summary = {
            "readyAuditSha256": ready_hash,
            "publishedAuditSha256": json_sha256(published_audit),
            "grantAuditSha256": json_sha256(grant_audit),
            "compatibilityAuditSha256": json_sha256(compatibility_audit),
            "runManifestSha256": grant_audit["runManifestSha256"],
            "fullRunBindingSha256": grant_audit["fullRunBindingSha256"],
            "priorPointerSha256": published_audit["priorPointerSha256"],
            "newPointerSha256": grant_audit["newPointerSha256"],
        }
        journal = self.ledger.record(
            journal,
            event="root_committed",
            payload=root_summary,
            update=lambda changed: (
                changed.update({"root": root_summary, "lastError": None}),
                _set_phase(changed, "COMMITTED"),
            ),
        )
        final_payload = self._final_payload(
            journal=journal,
            root_summary=root_summary,
        )
        self.ledger.write_final_receipt(final_payload)
        return {
            "ok": True,
            "status": "committed",
            "runId": self.config.run_id,
            "entryCount": self.config.expected_entry_count,
            "zeroRounds": 2,
            "runManifestSha256": root_summary["runManifestSha256"],
            "fullRunBindingSha256": root_summary["fullRunBindingSha256"],
        }

    def run(self) -> dict[str, Any]:
        lock_path = self.config.archive_root / ".channel_backup.lock"
        manager = _call_adapter(
            self.adapter.open_slot,
            archive_root=self.config.archive_root,
            lock_path=lock_path,
        )
        if not isinstance(manager, AbstractContextManager):
            raise FullRebuildError("rich_core_contract_unsupported")
        with manager as slot_value:
            slot = require_slot(slot_value)
            # Mutable state/queue are first opened only after the canonical
            # archive-root lock slot has entered.
            self._state_queue_bytes()
            begin = FullRebuildBeginRequestV3(
                schema_version=COORDINATOR_SCHEMA,
                run_id=self.config.run_id,
                archive_root=self.config.archive_root,
                expected_entry_count=self.config.expected_entry_count,
                expected_entry_set_sha256=self.config.expected_entry_set_sha256,
                expected_entries=self.config.expected_entries,
                guild_id=self.config.guild_id,
                timezone_name=self.config.timezone_name,
                baseline_sha256=self.config.baseline_sha256,
                state_sha256=self.config.expected_state_sha256,
                queue_sha256=self.config.expected_queue_sha256,
                adapter_code_sha256=self.config.adapter_code_sha256,
                rich_core_code_sha256=self.config.rich_core_code_sha256,
                coordinator_code_sha256=self.config.coordinator_code_sha256,
                configuration_sha256=self.config.configuration_sha256,
                expected_entries_artifact_sha256=(
                    self.config.expected_entries_artifact_sha256
                ),
                runtime_manifest_sha256=self.config.runtime_manifest_sha256,
                limits=self.config.limits.as_tuple(),
            )
            session_manager = _call_adapter(slot.begin_full_rebuild, begin)
            if not isinstance(session_manager, AbstractContextManager):
                raise FullRebuildError("rich_core_contract_unsupported")
            with session_manager as session_value:
                session = require_session(session_value)
                try:
                    return self._run_locked(session)
                except FullRebuildError as exc:
                    try:
                        journal = self.ledger.load() if self.ledger.exists() else None
                    except FullRebuildError:
                        journal = None
                    self._record_failure(
                        session,
                        journal,
                        exc.reason,
                        paused=exc.reason == "rich_convergence_exhausted",
                    )
                    raise
                except AdapterOperationError as exc:
                    error = FullRebuildError(exc.reason)
                    try:
                        journal = self.ledger.load() if self.ledger.exists() else None
                    except FullRebuildError:
                        journal = None
                    self._record_failure(session, journal, error.reason)
                    raise error from exc
                except Exception as exc:
                    error = FullRebuildError("unexpected_error")
                    try:
                        journal = self.ledger.load() if self.ledger.exists() else None
                    except FullRebuildError:
                        journal = None
                    self._record_failure(session, journal, error.reason)
                    raise error from exc


@dataclass(frozen=True)
class ProductionRuntimeBundleV1:
    adapter: ManagedRichCoreAdapterV3
    approved_baseline: ApprovedRebuildBaselineV2


def _component_bindings_from_manifest(
    *,
    skill_root: Path,
    components: Mapping[str, Any],
) -> tuple[RuntimeComponentBindingV1, ...]:
    if set(components) != set(RUNTIME_COMPONENT_PATHS):
        raise FullRebuildError("rich_core_integrity_mismatch")
    bindings: list[RuntimeComponentBindingV1] = []
    for name, expected_relative in RUNTIME_COMPONENT_PATHS.items():
        record = components.get(name)
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "sha256"}
            or record.get("path") != expected_relative
            or not HASH_RE.fullmatch(str(record.get("sha256") or ""))
        ):
            raise FullRebuildError("rich_core_integrity_mismatch")
        path = _lexical_absolute(skill_root / PurePosixPath(expected_relative))
        if not _contains(skill_root, path):
            raise FullRebuildError("rich_core_integrity_mismatch")
        safe = _require_integrity_component_path(skill_root=skill_root, path=path)
        if file_sha256(safe) != record["sha256"]:
            raise FullRebuildError("rich_core_integrity_mismatch")
        bindings.append(RuntimeComponentBindingV1(name, safe, record["sha256"]))
    return tuple(sorted(bindings, key=lambda item: item.name))


def _limits_from_canonical(value: Any) -> FullRebuildLimitsV2:
    if not isinstance(value, dict) or set(value) != {
        name for name, _default in FullRebuildLimitsV2().as_tuple()
    }:
        raise FullRebuildError("rich_core_integrity_mismatch")
    try:
        limits = FullRebuildLimitsV2(
            max_convergence_rounds=value["maxConvergenceRounds"],
            max_runtime_seconds=value["maxRuntimeSeconds"],
            max_requests=value["maxRequests"],
            max_retries=value["maxRetries"],
            max_asset_files=value["maxAssetFiles"],
            max_asset_bytes=value["maxAssetBytes"],
            minimum_free_space_bytes=value["minimumFreeSpaceBytes"],
        )
        _validate_limits(limits)
        return limits
    except (KeyError, TypeError, FullRebuildError) as exc:
        raise FullRebuildError("rich_core_integrity_mismatch") from exc


def _approved_baseline_from_integrity_bundle(
    *,
    manifest_path: Path,
    manifest_sha256: str,
    bindings: tuple[RuntimeComponentBindingV1, ...],
) -> ApprovedRebuildBaselineV2:
    by_name = {binding.name: binding for binding in bindings}
    config_value = _load_json(
        by_name["canonicalConfig"].path, "rich_core_integrity_mismatch"
    )
    entries_value = _load_json(
        by_name["expectedEntries"].path, "rich_core_integrity_mismatch"
    )
    config_keys = {
        "schemaVersion", "archiveRoot", "statePath", "queuePath",
        "baselineDir", "baselineSha256", "stateSha256", "queueSha256",
        "expectedEntryCount", "expectedEntrySetSha256",
        "expectedEntriesArtifactSha256", "guildId", "timezone", "limits",
    }
    entries_keys = {"schemaVersion", "entryCount", "entrySetSha256", "entries"}
    if (
        not isinstance(config_value, dict)
        or set(config_value) != config_keys
        or config_value.get("schemaVersion")
        != "openclaw-discord-full-rich-rebuild-config.v1"
        or not isinstance(config_value.get("expectedEntryCount"), int)
        or isinstance(config_value.get("expectedEntryCount"), bool)
        or config_value.get("expectedEntryCount") < 1
        or config_value.get("expectedEntriesArtifactSha256")
        != by_name["expectedEntries"].sha256
        or not isinstance(entries_value, dict)
        or set(entries_value) != entries_keys
        or entries_value.get("schemaVersion")
        != "openclaw-discord-full-rich-rebuild-entries.v1"
        or entries_value.get("entryCount")
        != config_value.get("expectedEntryCount")
        or entries_value.get("entrySetSha256")
        != config_value.get("expectedEntrySetSha256")
        or not isinstance(entries_value.get("entries"), list)
    ):
        raise FullRebuildError("rich_core_integrity_mismatch")
    entries = validate_expected_entries(
        entries_value["entries"],
        expected_count=config_value["expectedEntryCount"],
    )
    if entry_set_sha256(entries) != config_value["expectedEntrySetSha256"]:
        raise FullRebuildError("expected_entry_set_digest_mismatch")
    baseline = ApprovedRebuildBaselineV2(
        schema_version=APPROVED_BASELINE_SCHEMA,
        authority_mode="INTEGRITY_BOUND_V1",
        archive_root=_lexical_absolute(config_value["archiveRoot"]),
        state_path=_lexical_absolute(config_value["statePath"]),
        queue_path=_lexical_absolute(config_value["queuePath"]),
        baseline_dir=_lexical_absolute(config_value["baselineDir"]),
        baseline_sha256=config_value["baselineSha256"],
        expected_state_sha256=config_value["stateSha256"],
        expected_queue_sha256=config_value["queueSha256"],
        expected_entry_count=config_value["expectedEntryCount"],
        expected_entry_set_sha256=config_value["expectedEntrySetSha256"],
        expected_entries=entries,
        guild_id=config_value["guildId"],
        timezone_name=config_value["timezone"],
        adapter_code_sha256=by_name["adapter"].sha256,
        rich_core_code_sha256=by_name["richCore"].sha256,
        coordinator_code_sha256=by_name["coordinator"].sha256,
        configuration_sha256=by_name["canonicalConfig"].sha256,
        expected_entries_artifact_sha256=by_name["expectedEntries"].sha256,
        runtime_manifest_path=manifest_path,
        runtime_manifest_sha256=manifest_sha256,
        component_bindings=bindings,
        limits=_limits_from_canonical(config_value["limits"]),
        _authority=_PRODUCTION_AUTHORITY,
    )
    validated = _validate_approved_baseline(baseline)
    if _canonical_config_audit(validated) != config_value:
        raise FullRebuildError("rich_core_integrity_mismatch")
    if _expected_entries_artifact_audit(validated) != entries_value:
        raise FullRebuildError("rich_core_integrity_mismatch")
    return validated


def _load_production_bundle() -> ProductionRuntimeBundleV1:
    """Load one fixed, integrity-bound adapter/config/core bundle."""

    here = _require_safe_directory(Path(__file__).parent)
    skill_root = _require_safe_directory(here.parent)
    manifest_path = skill_root / PurePosixPath(RUNTIME_MANIFEST_RELATIVE_PATH)
    if not manifest_path.exists():
        raise FullRebuildError("rich_core_contract_pending")
    manifest_path = _require_integrity_component_path(
        skill_root=skill_root,
        path=manifest_path,
    )
    manifest_sha256 = file_sha256(manifest_path)
    manifest = _load_json(manifest_path, "rich_core_integrity_mismatch")
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schemaVersion", "adapterContract", "components"}
        or manifest.get("schemaVersion") != RUNTIME_MANIFEST_SCHEMA
        or manifest.get("adapterContract") != RICH_CORE_ADAPTER_VERSION
        or not isinstance(manifest.get("components"), dict)
    ):
        raise FullRebuildError("rich_core_integrity_mismatch")
    bindings = _component_bindings_from_manifest(
        skill_root=skill_root,
        components=manifest["components"],
    )
    baseline = _approved_baseline_from_integrity_bundle(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        bindings=bindings,
    )
    adapter_path = {binding.name: binding for binding in bindings}["adapter"].path
    module_name = "openclaw_managed_rich_core_adapter_v3"
    module = types.ModuleType(module_name)
    module.__file__ = str(adapter_path)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        # Compile the exact bytes whose SHA-256 was checked above.  The normal
        # import machinery may accept a timestamp-valid .pyc that is not part
        # of the runtime manifest.
        source = _safe_read_bytes(adapter_path)
        code = compile(source, str(adapter_path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except Exception as exc:  # noqa: S102 - integrity-bound fixed component
        sys.modules.pop(module_name, None)
        raise FullRebuildError("rich_core_integrity_mismatch") from exc
    adapter = require_adapter(getattr(module, "ADAPTER_V3", None))
    return ProductionRuntimeBundleV1(adapter=adapter, approved_baseline=baseline)


def _load_json(path: Path, reason: str) -> Any:
    try:
        return json.loads(_require_safe_file(path).read_text(encoding="utf-8"))
    except FullRebuildError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FullRebuildError(reason) from exc


def config_from_args(
    args: argparse.Namespace,
    *,
    approved_baseline: ApprovedRebuildBaselineV2,
) -> FullRebuildConfigV2:
    return validate_config(
        FullRebuildConfigV2(
            run_id=args.run_id,
            approved_baseline=approved_baseline,
        )
    )


def execute(
    args: argparse.Namespace,
    *,
    adapter: ManagedRichCoreAdapterV3 | None = None,
    approved_baseline: ApprovedRebuildBaselineV2 | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if (adapter is None) != (approved_baseline is None):
        raise FullRebuildError("rich_core_contract_unsupported")
    if adapter is None:
        bundle = _load_production_bundle()
        selected_adapter = bundle.adapter
        selected_baseline = bundle.approved_baseline
    else:
        selected_adapter = adapter
        assert approved_baseline is not None
        selected_baseline = approved_baseline
    config = config_from_args(args, approved_baseline=selected_baseline)
    return FullRichRebuildCoordinatorV2(
        config,
        adapter=selected_adapter,
        fault_injector=fault_injector,
    ).run()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coordinate one exact, resumable full rich Discord archive rebuild."
    )
    parser.add_argument("--run-id", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = execute(parse_args(argv))
        code = 0
    except FullRebuildError as exc:
        result = {"ok": False, "status": "blocked", "reason": exc.reason}
        code = 2
    except Exception:
        result = {"ok": False, "status": "blocked", "reason": "unexpected_error"}
        code = 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AdapterOperationError",
    "ApprovedRebuildBaselineV2",
    "BaselineCapabilityV2",
    "BaselineVerificationRequestV2",
    "CompatibilityPublishRequestV2",
    "CommittedRunInspectionCapabilityV2",
    "CommittedRunInspectionRequestV2",
    "EntryBindingV2",
    "EntryStageRequestV3",
    "FullRebuildBeginRequestV3",
    "FullRebuildConfigV2",
    "FullRebuildError",
    "FullRebuildLimitsV2",
    "FullRebuildSessionV2",
    "FullRichRebuildCoordinatorV2",
    "FullRoundCapabilityV2",
    "FullRunReadyCapabilityV2",
    "FullStageCapabilityV2",
    "InventoryCapabilityV3",
    "InventoryRequestV3",
    "LockedRichSlotV3",
    "ManagedRichCoreAdapterV3",
    "PermissionCapabilityV2",
    "PermissionRequestV2",
    "PreparedFullStageCapabilityV2",
    "ReservedFullStageCapabilityV2",
    "ResourceReservationRequestV2",
    "RootCommitGrantV2",
    "RootCurrentCapabilityV2",
    "RootPublishRequestV2",
    "RuntimeComponentBindingV1",
    "RunResourcesCapabilityV2",
    "SUPPORTED_RICH_CORE_CONTRACT",
    "SealedFullEntryCapabilityV2",
    "entry_set_sha256",
    "execute",
    "validate_expected_entries",
]
