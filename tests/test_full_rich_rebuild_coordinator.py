from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from contextlib import AbstractContextManager
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).parents[1]
    / "skill/openclaw-discord-server-backup/scripts/run_full_rich_rebuild_v2.py"
)
spec = importlib.util.spec_from_file_location("run_full_rich_rebuild_v2", SCRIPT)
full = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = full
spec.loader.exec_module(full)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
OBSERVED = "2026-09-05T08:00:00Z"


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def entry_values(count: int) -> list[dict[str, object]]:
    classes = full.REQUIRED_INVENTORY_CLASSES
    return [
        {
            "channelId": str(1_490_000_000_000_000_000 + index),
            "relativePath": f"group/entry-{index:03d}",
            "type": "thread" if index % 2 else "channel",
            "parentChannelId": (
                str(1_480_000_000_000_000_000 + index) if index % 2 else None
            ),
            "inventoryClass": classes[index % len(classes)],
        }
        for index in range(1, count + 1)
    ]


def make_config(tmp_path: Path, *, count: int = 3, max_zero_rounds: int = 8):
    archive = tmp_path / "archive"
    baseline = tmp_path / "baseline"
    archive.mkdir(parents=True)
    baseline.mkdir(parents=True)
    state = tmp_path / "state.json"
    queue = tmp_path / "queue.json"
    state.write_bytes(b'{"entries":{}}\n')
    queue.write_bytes(b'{"items":[]}\n')
    entries = full.validate_expected_entries(entry_values(count), expected_count=count)
    approved = full.ApprovedRebuildBaselineV2(
        schema_version=full.APPROVED_BASELINE_SCHEMA,
        authority_mode="TEST_ONLY",
        archive_root=archive,
        state_path=state,
        queue_path=queue,
        baseline_dir=baseline,
        baseline_sha256=HASH_A,
        expected_state_sha256=digest_bytes(state.read_bytes()),
        expected_queue_sha256=digest_bytes(queue.read_bytes()),
        expected_entry_count=count,
        expected_entry_set_sha256=full.entry_set_sha256(entries),
        expected_entries=entries,
        guild_id="1476493755426017414",
        timezone_name="Asia/Taipei",
        adapter_code_sha256=HASH_B,
        rich_core_code_sha256=HASH_A,
        coordinator_code_sha256=HASH_D,
        configuration_sha256=HASH_C,
        expected_entries_artifact_sha256=HASH_D,
        runtime_manifest_path=None,
        runtime_manifest_sha256=HASH_A,
        component_bindings=(),
        limits=full.FullRebuildLimitsV2(
            max_convergence_rounds=max_zero_rounds,
            max_runtime_seconds=3600,
            max_requests=100_000,
            max_retries=1_000,
            max_asset_files=10_000,
            max_asset_bytes=1024**3,
            minimum_free_space_bytes=0,
        ),
    )
    return full.FullRebuildConfigV2(
        run_id="run-20260905",
        approved_baseline=approved,
    )


def make_integrity_bound_runtime(tmp_path: Path, monkeypatch):
    skill_root = tmp_path / "installed-skill"
    scripts = skill_root / "scripts"
    manifests = skill_root / "manifests"
    scripts.mkdir(parents=True, mode=0o700)
    manifests.mkdir(mode=0o700)
    archive = tmp_path / "archive"
    baseline_dir = tmp_path / "baseline"
    archive.mkdir(mode=0o700)
    baseline_dir.mkdir(mode=0o700)
    state = tmp_path / "state.json"
    queue = tmp_path / "queue.json"
    state.write_bytes(b'{"entries":{}}\n')
    queue.write_bytes(b'{"items":[]}\n')

    entries = full.validate_expected_entries(
        entry_values(full.PRODUCTION_ENTRY_COUNT),
        expected_count=full.PRODUCTION_ENTRY_COUNT,
    )
    entry_digest = full.entry_set_sha256(entries)
    entries_path = manifests / "full-rich-rebuild-entries.v1.json"
    entries_payload = {
        "schemaVersion": "openclaw-discord-full-rich-rebuild-entries.v1",
        "entryCount": full.PRODUCTION_ENTRY_COUNT,
        "entrySetSha256": entry_digest,
        "entries": [entry.audit_record() for entry in entries],
    }
    entries_path.write_bytes(full.canonical_json_bytes(entries_payload) + b"\n")
    entries_path.chmod(0o600)

    limits = full.FullRebuildLimitsV2(minimum_free_space_bytes=0)
    config_path = manifests / "full-rich-rebuild-config.v1.json"
    config_payload = {
        "schemaVersion": "openclaw-discord-full-rich-rebuild-config.v1",
        "archiveRoot": str(archive),
        "statePath": str(state),
        "queuePath": str(queue),
        "baselineDir": str(baseline_dir),
        "baselineSha256": HASH_A,
        "stateSha256": digest_bytes(state.read_bytes()),
        "queueSha256": digest_bytes(queue.read_bytes()),
        "expectedEntryCount": full.PRODUCTION_ENTRY_COUNT,
        "expectedEntrySetSha256": entry_digest,
        "expectedEntriesArtifactSha256": full.file_sha256(entries_path),
        "guildId": "1476493755426017414",
        "timezone": "Asia/Taipei",
        "limits": dict(limits.as_tuple()),
    }
    config_path.write_bytes(full.canonical_json_bytes(config_payload) + b"\n")
    config_path.chmod(0o600)

    coordinator_path = scripts / "run_full_rich_rebuild_v2.py"
    coordinator_path.write_bytes(SCRIPT.read_bytes())
    coordinator_path.chmod(0o700)
    rich_core_path = scripts / "rich_message_archive.py"
    rich_core_path.write_text("# integrity-bound rich core fixture\n", encoding="utf-8")
    rich_core_path.chmod(0o600)
    adapter_path = scripts / "rich_core_adapter_v3.py"
    adapter_path.write_text(
        "import run_full_rich_rebuild_v2 as coordinator\n"
        "class Adapter(coordinator.ManagedRichCoreAdapterV3):\n"
        "    pass\n"
        "ADAPTER_V3 = Adapter()\n",
        encoding="utf-8",
    )
    adapter_path.chmod(0o600)

    component_paths = {
        "adapter": adapter_path,
        "richCore": rich_core_path,
        "coordinator": coordinator_path,
        "canonicalConfig": config_path,
        "expectedEntries": entries_path,
    }
    runtime_manifest = {
        "schemaVersion": full.RUNTIME_MANIFEST_SCHEMA,
        "adapterContract": full.RICH_CORE_ADAPTER_VERSION,
        "components": {
            name: {
                "path": full.RUNTIME_COMPONENT_PATHS[name],
                "sha256": full.file_sha256(path),
            }
            for name, path in component_paths.items()
        },
    }
    manifest_path = manifests / "runtime-components.v1.json"
    manifest_path.write_bytes(full.canonical_json_bytes(runtime_manifest) + b"\n")
    manifest_path.chmod(0o600)
    monkeypatch.setattr(full, "__file__", str(coordinator_path))
    return component_paths


class CapabilityMixin:
    def __init__(self, audit):
        self.audit = audit


class Inventory(CapabilityMixin, full.InventoryCapabilityV3):
    pass


class Permissions(CapabilityMixin, full.PermissionCapabilityV2):
    pass


class Baseline(CapabilityMixin, full.BaselineCapabilityV2):
    pass


class Resources(CapabilityMixin, full.RunResourcesCapabilityV2):
    pass


class Stage(CapabilityMixin, full.FullStageCapabilityV2):
    pass


class Reserved(CapabilityMixin, full.ReservedFullStageCapabilityV2):
    pass


class Prepared(CapabilityMixin, full.PreparedFullStageCapabilityV2):
    pass


class Sealed(CapabilityMixin, full.SealedFullEntryCapabilityV2):
    pass


class Round(CapabilityMixin, full.FullRoundCapabilityV2):
    pass


class Ready(CapabilityMixin, full.FullRunReadyCapabilityV2):
    pass


class RootCurrent(CapabilityMixin, full.RootCurrentCapabilityV2):
    pass


class RootGrant(CapabilityMixin, full.RootCommitGrantV2):
    pass


class CommittedInspection(
    CapabilityMixin, full.CommittedRunInspectionCapabilityV2
):
    pass


class Manager(AbstractContextManager):
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeSession(full.FullRebuildSessionV2):
    def __init__(self, adapter, begin):
        self.adapter = adapter
        self.begin = begin
        self.current_round_entries = []

    def assert_canonical_lock_held(self):
        held = self.adapter.lock_entered
        self.adapter.lock_observations.append(held)
        if not held:
            raise full.AdapterOperationError("rich_core_authority_invalid")
        return None

    def describe_capability(self, capability):
        return capability.audit

    def verify_immutable_baseline(self, request):
        assert request.expected_baseline_sha256 == self.begin.baseline_sha256
        return Baseline({
            "schemaVersion": full.BASELINE_AUDIT_SCHEMA,
            "runId": request.run_id,
            "status": "AUDIT_VERIFIED",
            "baselineSha256": request.expected_baseline_sha256,
            "archiveManifestSha256": HASH_D,
            "stateSha256": request.expected_state_sha256,
            "queueSha256": request.expected_queue_sha256,
            "verificationSha256": HASH_A,
            "verifiedAt": OBSERVED,
            "errors": [],
        })

    def collect_fresh_inventory(self, request):
        self.adapter.calls.append(("inventory", request.round_id))
        bindings = [entry.audit_record() for entry in request.expected_entries]
        if self.adapter.inventory_identity_drift:
            bindings = list(reversed(bindings))
        count = (
            self.adapter.inventory_count
            if self.adapter.inventory_count is not None
            else len(bindings)
        )
        endpoints = {
            name: {"complete": True, "terminalPage": True, "errorCount": 0}
            for name in full.REQUIRED_INVENTORY_CLASSES
        }
        if self.adapter.pagination_incomplete:
            endpoints["archived_private_threads"]["terminalPage"] = False
        audit = {
            "schemaVersion": full.INVENTORY_AUDIT_SCHEMA,
            "runId": request.run_id,
            "roundId": request.round_id,
            "roundKind": request.round_kind,
            "observedAt": OBSERVED,
            "guildId": request.guild_id,
            "complete": not self.adapter.inventory_incomplete,
            "truncated": self.adapter.inventory_incomplete,
            "entryCount": count,
            "entrySetSha256": request.expected_entry_set_sha256,
            "inventoryDigest": HASH_B,
            "entryBindings": bindings,
            "endpointClasses": endpoints,
            "warnings": [],
            "errors": [],
        }
        return Inventory(audit)

    def prove_permissions(self, request, *, inventory):
        self.adapter.calls.append(("permissions", request.round_id))
        return Permissions({
            "schemaVersion": full.PERMISSION_AUDIT_SCHEMA,
            "runId": request.run_id,
            "roundId": request.round_id,
            "guildId": request.guild_id,
            "inventoryDigest": inventory.audit["inventoryDigest"],
            "entrySetSha256": request.expected_entry_set_sha256,
            "entryCount": request.expected_entry_count,
            "entriesPassed": request.expected_entry_count,
            "applicationIdSha256": HASH_C,
            "messageContentEffective": not self.adapter.message_content_missing,
            "runtimeEvidenceOneShot": True,
            "permissionErrors": 0 if not self.adapter.message_content_missing else 1,
            "evidenceSha256": HASH_D,
            "observedAt": OBSERVED,
        })

    def acquire_run_resources(self, request, *, baseline, inventory):
        self.adapter.calls.append(("resources", request.resume_audit_sha256))
        return Resources({
            "schemaVersion": full.RESOURCE_AUDIT_SCHEMA,
            "runId": request.run_id,
            "status": "RESERVED",
            "assetBudgetIdentitySha256": HASH_A,
            "diskReservationIdentitySha256": HASH_B,
            "reservedBytes": 4096,
            "allocatedBytes": 4096,
            "minimumFreeSpaceBytes": request.minimum_free_space_bytes,
            "maxAssetFiles": request.max_asset_files,
            "maxAssetBytes": request.max_asset_bytes,
            "consumedAssetFiles": 0,
            "consumedAssetBytes": 0,
            "errors": [],
        })

    def _sealed_audit(self, request):
        zero_attempt = max(0, request.sequence * 0)
        if request.round_kind == "baseline":
            new_ids = 1
            mutable = 0
        elif request.round_kind == "delta":
            new_ids = 0
            mutable = 0
        else:
            attempt = max(0, int(request.round_id.split("-")[1]) - 3)
            plan_value = (
                self.adapter.zero_plan[attempt]
                if attempt < len(self.adapter.zero_plan)
                else 0
            )
            new_ids = plan_value if request.sequence == 1 else 0
            mutable = 0
        counts = {
            "liveIds": 1,
            "canonicalIds": 1,
            "visiblePointers": 1,
            "markdownBlocks": 1,
            "inScopeAssets": 0,
            "verifiedAssets": 0,
            "duplicateCanonicalIds": 0,
            "unknownVisibleFields": 0,
            "attachmentErrors": 0,
            "liveErrors": 0,
            "paginationErrors": 0,
            "newIds": new_ids,
            "mutableChanges": mutable,
            "unresolvedAssetRefreshes": 0,
        }
        return {
            "schemaVersion": full.SEALED_ENTRY_AUDIT_SCHEMA,
            "runId": request.run_id,
            "roundId": request.round_id,
            "roundKind": request.round_kind,
            "entryBinding": request.entry.audit_record(),
            "generationId": f"{request.round_id}-{request.entry.channel_id}",
            "generationSha256": HASH_A,
            "entryReceiptSha256": HASH_B,
            "liveEvidenceSha256": HASH_C,
            "cutoff": "1540000000000000001",
            "trueEmpty": False,
            "explicitEmptyProof": False,
            "terminalPageProof": not self.adapter.pagination_incomplete,
            "runtimeEvidenceConsumed": True,
            "perEntryCurrentMutated": self.adapter.mutate_entry_current_early,
            "counts": counts,
        }

    def collect_and_stage_full_snapshot(
        self, request, *, prior_round, inventory, permissions, resources
    ):
        self.adapter.calls.append(("stage", request.round_id, request.entry.channel_id))
        stage = Stage({"request": request})
        if self.adapter.replay_stage:
            if self.adapter.replayed_stage is None:
                self.adapter.replayed_stage = stage
            return self.adapter.replayed_stage
        return stage

    def reserve_full_stage_assets(self, stage, *, resources):
        self.adapter.calls.append(("reserve", stage.audit["request"].entry.channel_id))
        if self.adapter.quota_failure:
            raise full.AdapterOperationError("rich_asset_budget_exhausted")
        return Reserved(stage.audit)

    def install_full_pass_evidence(self, reserved, *, inventory, permissions):
        request = reserved.audit["request"]
        self.adapter.calls.append(("evidence", request.entry.channel_id))
        return Prepared(reserved.audit)

    def seal_full_entry(self, prepared):
        request = prepared.audit["request"]
        self.adapter.calls.append(("seal", request.round_id, request.entry.channel_id))
        audit = self._sealed_audit(request)
        sealed = Sealed(audit)
        self.adapter.entry_audits[full.json_sha256(audit)] = audit
        if self.adapter.replay_sealed:
            if self.adapter.replayed_sealed is None:
                self.adapter.replayed_sealed = sealed
            return self.adapter.replayed_sealed
        return sealed

    def recover_sealed_entry(
        self, request, *, prior_round, inventory, permissions, resources
    ):
        self.adapter.calls.append(("recover_entry", request.round_id, request.entry.channel_id))
        audit = self.adapter.entry_audits[request.sealed_audit_sha256]
        return Sealed(audit)

    def seal_round(self, request, *, inventory, permissions, sealed_entries):
        self.adapter.calls.append(("seal_round", request.round_id))
        audits = [entry.audit for entry in sealed_entries]
        counts = full._round_counts(audits)
        hashes = dict(request.sealed_audit_sha256_by_channel)
        zero = (
            counts["newIds"] == 0
            and counts["mutableChanges"] == 0
            and counts["unresolvedAssetRefreshes"] == 0
        )
        audit = {
            "schemaVersion": full.ROUND_AUDIT_SCHEMA,
            "runId": request.run_id,
            "roundId": request.round_id,
            "roundKind": request.round_kind,
            "entryCount": request.expected_entry_count,
            "entrySetSha256": request.expected_entry_set_sha256,
            "inventoryDigest": inventory.audit["inventoryDigest"],
            "sealedEntryAuditSha256ByChannel": hashes,
            "counts": counts,
            "zeroDelta": zero,
            "explicitTerminalForEveryEntry": not self.adapter.pagination_incomplete,
            "runtimeEvidenceFresh": True,
            "inventoryStable": not self.adapter.inventory_drift_at_seal,
            "stateQueueInvariant": True,
            "capabilitiesConsumed": True,
            "roundReceiptSha256": HASH_D,
        }
        capability = Round(audit)
        self.adapter.round_audits[full.json_sha256(audit)] = audit
        return capability

    def recover_sealed_round(self, request, *, resources):
        self.adapter.calls.append(("recover_round", request.round_id))
        return Round(self.adapter.round_audits[request.round_audit_sha256])

    def finalize_full_rebuild(self, request, *, rounds, resources):
        self.adapter.calls.append(("finalize", len(rounds)))
        if self.adapter.final_entry_count is not None:
            count = self.adapter.final_entry_count
        else:
            count = request.expected_entry_count
        coverage = {
            name: {"expected": count, "verified": count}
            for name in ("id", "visible", "markdown", "binary")
        }
        return Ready({
            "schemaVersion": full.RUN_READY_AUDIT_SCHEMA,
            "runId": request.run_id,
            "status": "RUNTIME_READY",
            "entryCount": count,
            "entrySetSha256": request.expected_entry_set_sha256,
            "roundAuditSha256s": list(request.round_audit_sha256s),
            "zeroRoundStreak": request.zero_round_streak,
            "stateSha256": request.state_sha256,
            "queueSha256": request.queue_sha256,
            "coverage": coverage,
            "liveErrors": 0,
            "paginationErrors": 0,
            "unknownVisibleFields": 0,
            "attachmentErrors": 0,
            "runtimeEvidenceConsumed": True,
            "runManifestSha256": HASH_A,
            "fullRunBindingSha256": HASH_B,
        })

    def publish_root_run_current(self, request, *, ready):
        self.adapter.calls.append(("root_publish", request.run_id))
        self.adapter.root_published = True
        return RootCurrent({
            "schemaVersion": full.ROOT_CURRENT_AUDIT_SCHEMA,
            "runId": request.run_id,
            "published": True,
            "runManifestSha256": ready.audit["runManifestSha256"],
            "fullRunBindingSha256": ready.audit["fullRunBindingSha256"],
            "priorPointerSha256": self.adapter.prior_pointer_sha256,
            "newPointerSha256": HASH_C,
            "directoryFsync": True,
            "rollbackPrepared": True,
        })

    def inspect_root_run_current(self, request, *, ready, published):
        self.adapter.calls.append(("root_readback", request.run_id))
        self.adapter.root_readback = True
        return RootGrant({
            "schemaVersion": full.ROOT_GRANT_AUDIT_SCHEMA,
            "runId": request.run_id,
            "readback": True,
            "runManifestSha256": published.audit["runManifestSha256"],
            "fullRunBindingSha256": published.audit["fullRunBindingSha256"],
            "newPointerSha256": published.audit["newPointerSha256"],
            "selectedEntryCount": request.expected_entry_count,
            "entrySetSha256": request.expected_entry_set_sha256,
            "readerIndexerCanary": True,
            "rollbackVerified": True,
        })

    def inspect_committed_full_rebuild(self, request):
        self.adapter.calls.append(("committed_readback", request.run_id))
        prior = (
            self.adapter.committed_prior_pointer_override
            if self.adapter.committed_prior_pointer_override is not None
            else request.prior_pointer_sha256
        )
        return CommittedInspection({
            "schemaVersion": full.COMMITTED_READBACK_AUDIT_SCHEMA,
            "runId": request.run_id,
            "readOnly": True,
            "runManifestSha256": request.run_manifest_sha256,
            "fullRunBindingSha256": request.full_run_binding_sha256,
            "priorPointerSha256": prior,
            "newPointerSha256": request.new_pointer_sha256,
            "selectedEntryCount": request.expected_entry_count,
            "entrySetSha256": request.expected_entry_set_sha256,
            "readerIndexerCanary": True,
            "finalReceiptSha256": request.final_receipt_sha256,
            "stateQueueInvariant": True,
        })

    def publish_compatibility_currents(self, request, *, grant):
        self.adapter.calls.append(("compatibility", request.run_id))
        assert self.adapter.root_published and self.adapter.root_readback
        self.adapter.compatibility_published = True
        return {
            "schemaVersion": full.COMPATIBILITY_AUDIT_SCHEMA,
            "runId": request.run_id,
            "status": "AUDIT_ONLY",
            "entryCount": request.expected_entry_count,
            "updatedEntries": request.expected_entry_count,
            "verifiedEntries": request.expected_entry_count,
            "authoritativeRootUnchanged": True,
            "errors": [],
        }


class FakeSlot(full.LockedRichSlotV3):
    def __init__(self, adapter):
        self.adapter = adapter

    def begin_full_rebuild(self, request):
        self.adapter.calls.append(("begin", request.run_id))
        return Manager(FakeSession(self.adapter, request))


class FakeAdapter(full.ManagedRichCoreAdapterV3):
    def __init__(self):
        self.calls = []
        self.inventory_count = None
        self.inventory_incomplete = False
        self.inventory_identity_drift = False
        self.inventory_drift_at_seal = False
        self.pagination_incomplete = False
        self.message_content_missing = False
        self.mutate_entry_current_early = False
        self.quota_failure = False
        self.replay_stage = False
        self.replayed_stage = None
        self.replay_sealed = False
        self.replayed_sealed = None
        self.zero_plan = [0, 0]
        self.final_entry_count = None
        self.entry_audits = {}
        self.round_audits = {}
        self.root_published = False
        self.root_readback = False
        self.compatibility_published = False
        self.lock_entered = False
        self.lock_observations = []
        self.prior_pointer_sha256 = HASH_D
        self.committed_prior_pointer_override = None

    def open_slot(self, *, archive_root, lock_path):
        assert lock_path == archive_root / ".channel_backup.lock"
        self.calls.append(("open_slot", str(lock_path)))
        adapter = self

        class SlotManager(Manager):
            def __enter__(self):
                adapter.lock_entered = True
                return super().__enter__()

            def __exit__(self, exc_type, exc, traceback):
                try:
                    return super().__exit__(exc_type, exc, traceback)
                finally:
                    adapter.lock_entered = False

        return SlotManager(FakeSlot(self))


def run(config, adapter=None, fault=None):
    adapter = adapter or FakeAdapter()
    result = full.FullRichRebuildCoordinatorV2(
        config, adapter=adapter, fault_injector=fault
    ).run()
    return result, adapter


def test_success_runs_baseline_delta_two_zero_rounds_then_root_and_compatibility(tmp_path):
    config = make_config(tmp_path)
    state_before = config.state_path.read_bytes()
    queue_before = config.queue_path.read_bytes()
    result, adapter = run(config)

    assert result["status"] == "committed"
    assert result["entryCount"] == 3
    assert config.state_path.read_bytes() == state_before
    assert config.queue_path.read_bytes() == queue_before
    round_calls = [call for call in adapter.calls if call[0] == "seal_round"]
    assert [call[1] for call in round_calls] == [
        "round-0001-baseline",
        "round-0002-delta",
        "round-0003-zero",
        "round-0004-zero",
    ]
    root_index = next(i for i, call in enumerate(adapter.calls) if call[0] == "root_publish")
    readback_index = next(i for i, call in enumerate(adapter.calls) if call[0] == "root_readback")
    compat_index = next(i for i, call in enumerate(adapter.calls) if call[0] == "compatibility")
    assert root_index < readback_index < compat_index
    journal = full._load_envelope(
        config.archive_root / "runs" / config.run_id / "run-journal.json",
        full.JOURNAL_ENVELOPE_SCHEMA,
        "journal_corrupt",
    )
    assert journal["phase"] == "COMMITTED"
    assert journal["zeroRoundStreak"] == 2
    assert len(journal["rounds"]) == 4
    receipt = json.loads(
        (
            config.archive_root
            / "runs"
            / config.run_id
            / "receipts/coordinator/full-rebuild-run.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["status"] == "AUDIT_ONLY"
    assert "PASS" not in json.dumps(receipt)


@pytest.mark.parametrize("actual_count", [177, 179])
def test_exact_inventory_rejects_177_or_179_against_expected_178(tmp_path, actual_count):
    config = make_config(tmp_path, count=178)
    adapter = FakeAdapter()
    adapter.inventory_count = actual_count
    with pytest.raises(full.FullRebuildError, match="inventory_count_mismatch"):
        run(config, adapter)
    assert not adapter.root_published


def test_self_consistent_177_entry_production_authority_is_rejected(tmp_path):
    test_config = make_config(tmp_path, count=177)
    values = dict(test_config.approved_baseline.__dict__)
    values.update({
        "authority_mode": "INTEGRITY_BOUND_V1",
        "_authority": full._PRODUCTION_AUTHORITY,
    })
    production_baseline = full.ApprovedRebuildBaselineV2(**values)
    config = full.FullRebuildConfigV2(
        run_id=test_config.run_id,
        approved_baseline=production_baseline,
    )
    with pytest.raises(full.FullRebuildError, match="expected_entry_count_mismatch"):
        full.FullRichRebuildCoordinatorV2(config, adapter=FakeAdapter())


def test_inventory_drift_and_non_terminal_pagination_fail_closed(tmp_path):
    config = make_config(tmp_path / "identity")
    adapter = FakeAdapter()
    adapter.inventory_identity_drift = True
    with pytest.raises(full.FullRebuildError, match="inventory_incomplete"):
        run(config, adapter)
    assert not adapter.root_published

    config = make_config(tmp_path / "pagination")
    adapter = FakeAdapter()
    adapter.pagination_incomplete = True
    with pytest.raises(full.FullRebuildError, match="inventory_incomplete"):
        run(config, adapter)
    assert not adapter.root_published


def test_failure_receipt_is_written_before_canonical_lock_release(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    adapter.message_content_missing = True
    with pytest.raises(full.FullRebuildError, match="message_content_unavailable"):
        run(config, adapter)
    journal = full._load_envelope(
        config.archive_root / "runs" / config.run_id / "run-journal.json",
        full.JOURNAL_ENVELOPE_SCHEMA,
        "journal_corrupt",
    )
    assert journal["receiptChain"][-1]["event"] == "run_failed"
    assert adapter.lock_observations
    assert all(adapter.lock_observations)
    assert adapter.lock_entered is False

def test_inventory_drift_at_round_seal_blocks_cutover(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    adapter.inventory_drift_at_seal = True
    with pytest.raises(full.FullRebuildError, match="inventory_drift"):
        run(config, adapter)
    assert not adapter.root_published


def test_no_per_entry_current_mutation_before_root_authority(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    adapter.mutate_entry_current_early = True
    with pytest.raises(full.FullRebuildError, match="rich_entry_current_mutated_early"):
        run(config, adapter)
    assert not adapter.root_published
    assert not adapter.compatibility_published


def test_state_or_queue_drift_blocks_before_root_and_never_advances_cursor(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    original = adapter.open_slot

    def open_slot(**kwargs):
        manager = original(**kwargs)
        original_begin = manager.value.begin_full_rebuild

        def begin(request):
            inner = original_begin(request)
            session = inner.value
            original_seal = session.seal_full_entry

            def seal(prepared):
                value = original_seal(prepared)
                config.state_path.write_bytes(b'{"entries":{"cursor":"advanced"}}\n')
                return value

            session.seal_full_entry = seal
            return inner

        manager.value.begin_full_rebuild = begin
        return manager

    adapter.open_slot = open_slot
    with pytest.raises(full.FullRebuildError, match="state_queue_drift"):
        run(config, adapter)
    assert not adapter.root_published


def test_runtime_capability_replay_is_rejected(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    adapter.replay_stage = True
    with pytest.raises(full.FullRebuildError, match="rich_core_authority_replayed"):
        run(config, adapter)
    assert not adapter.root_published


def test_shared_quota_failure_blocks_before_any_seal_or_root(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    adapter.quota_failure = True
    with pytest.raises(full.FullRebuildError, match="rich_asset_budget_exhausted"):
        run(config, adapter)
    assert not any(call[0] == "seal" for call in adapter.calls)
    assert not adapter.root_published


def test_zero_round_change_resets_streak_and_requires_two_new_zero_rounds(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    adapter.zero_plan = [0, 1, 0, 0]
    result, adapter = run(config, adapter)
    assert result["status"] == "committed"
    rounds = [call[1] for call in adapter.calls if call[0] == "seal_round"]
    assert rounds[-4:] == [
        "round-0003-zero",
        "round-0004-zero",
        "round-0005-zero",
        "round-0006-zero",
    ]


def test_convergence_bound_pauses_without_root_cutover(tmp_path):
    config = make_config(tmp_path, max_zero_rounds=2)
    adapter = FakeAdapter()
    adapter.zero_plan = [1, 1, 1]
    with pytest.raises(full.FullRebuildError, match="rich_convergence_exhausted"):
        run(config, adapter)
    assert not adapter.root_published
    journal = full._load_envelope(
        config.archive_root / "runs" / config.run_id / "run-journal.json",
        full.JOURNAL_ENVELOPE_SCHEMA,
        "journal_corrupt",
    )
    assert journal["phase"] == "PAUSED"


class SimulatedCrash(BaseException):
    pass


def test_crash_after_seal_before_journal_retries_without_selecting_partial_run(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    fired = {"value": False}

    def fault(point):
        if point.startswith("after_seal_before_journal") and not fired["value"]:
            fired["value"] = True
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        run(config, adapter, fault)
    assert not adapter.root_published
    result, adapter = run(config, adapter)
    assert result["status"] == "committed"


def test_crash_after_entry_journal_recovers_sealed_entry_by_checksum(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    fired = {"value": False}

    def fault(point):
        if point == "after_journal_write" and not fired["value"]:
            # Ignore initial journal; crash after the first event-backed journal.
            fired["value"] = True
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        run(config, adapter, fault)
    assert not adapter.root_published
    result, adapter = run(config, adapter)
    assert result["status"] == "committed"


def test_tampered_journal_checksum_blocks_resume(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    fired = {"value": False}

    def fault(point):
        if point.startswith("after_seal_before_journal") and not fired["value"]:
            fired["value"] = True
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        run(config, adapter, fault)
    journal_path = config.archive_root / "runs" / config.run_id / "run-journal.json"
    envelope = json.loads(journal_path.read_text(encoding="utf-8"))
    envelope["payload"]["phase"] = "COMMITTED"
    journal_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(full.FullRebuildError, match="journal_corrupt"):
        run(config, adapter)


def test_crash_after_root_publish_retries_idempotently_before_compatibility(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    fired = {"value": False}

    def fault(point):
        if point == "after_root_publish_before_readback" and not fired["value"]:
            fired["value"] = True
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        run(config, adapter, fault)
    assert adapter.root_published
    assert not adapter.root_readback
    assert not adapter.compatibility_published
    result, adapter = run(config, adapter)
    assert result["status"] == "committed"
    assert adapter.root_readback and adapter.compatibility_published


def test_committed_rerun_is_read_only_and_idempotent(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    run(config, adapter)
    journal_path = config.archive_root / "runs" / config.run_id / "run-journal.json"
    receipt_path = (
        config.archive_root
        / "runs"
        / config.run_id
        / "receipts/coordinator/full-rebuild-run.json"
    )
    events_path = receipt_path.parent / "events"
    before = (
        journal_path.read_bytes(),
        receipt_path.read_bytes(),
        sorted(path.name for path in events_path.iterdir()),
    )
    call_offset = len(adapter.calls)
    result, _adapter = run(config, adapter)
    after_calls = adapter.calls[call_offset:]
    after = (
        journal_path.read_bytes(),
        receipt_path.read_bytes(),
        sorted(path.name for path in events_path.iterdir()),
    )
    assert result["idempotentReadback"] is True
    assert before == after
    assert [call[0] for call in after_calls if call[0] in {
        "root_publish", "root_readback", "compatibility", "finalize",
    }] == []
    assert [call[0] for call in after_calls].count("committed_readback") == 1


def test_committed_rerun_changed_prior_pointer_fails_without_mutation(tmp_path):
    config = make_config(tmp_path)
    adapter = FakeAdapter()
    run(config, adapter)
    journal_path = config.archive_root / "runs" / config.run_id / "run-journal.json"
    receipt_path = (
        config.archive_root
        / "runs"
        / config.run_id
        / "receipts/coordinator/full-rebuild-run.json"
    )
    events_path = receipt_path.parent / "events"
    before = (
        journal_path.read_bytes(),
        receipt_path.read_bytes(),
        sorted(path.name for path in events_path.iterdir()),
    )
    adapter.committed_prior_pointer_override = HASH_A
    with pytest.raises(full.FullRebuildError, match="committed_readback_failed"):
        run(config, adapter)
    after = (
        journal_path.read_bytes(),
        receipt_path.read_bytes(),
        sorted(path.name for path in events_path.iterdir()),
    )
    assert before == after


def test_finalizer_rejects_177_of_178_even_after_rounds(tmp_path):
    config = make_config(tmp_path, count=178)
    adapter = FakeAdapter()
    adapter.final_entry_count = 177
    with pytest.raises(full.FullRebuildError, match="rich_full_run_incomplete"):
        run(config, adapter)
    assert not adapter.root_published


def test_production_entrypoint_fails_closed_without_exact_adapter_v3(tmp_path):
    del tmp_path
    args = full.parse_args(["--run-id", "run-20260905"])
    with pytest.raises(full.FullRebuildError, match="rich_core_contract_pending"):
        full.execute(args)


def test_integrity_bundle_binds_all_components_and_rechecks_under_lock(
    tmp_path, monkeypatch
):
    paths = make_integrity_bound_runtime(tmp_path, monkeypatch)
    bundle = full._load_production_bundle()
    baseline = bundle.approved_baseline
    assert baseline.production_authorized
    assert baseline.expected_entry_count == full.PRODUCTION_ENTRY_COUNT
    assert {binding.name for binding in baseline.component_bindings} == set(
        full.RUNTIME_COMPONENT_PATHS
    )
    config = full.FullRebuildConfigV2(
        run_id="run-20260905",
        approved_baseline=baseline,
    )
    adapter = FakeAdapter()
    adapter.lock_entered = True
    session = FakeSession(adapter, None)
    full._verify_runtime_authority_under_lock(config, session=session)
    paths["adapter"].write_text("# post-load tamper\n", encoding="utf-8")
    paths["adapter"].chmod(0o600)
    with pytest.raises(full.FullRebuildError, match="rich_core_integrity_mismatch"):
        full._verify_runtime_authority_under_lock(config, session=session)


def test_integrity_bundle_rejects_group_writable_component(tmp_path, monkeypatch):
    paths = make_integrity_bound_runtime(tmp_path, monkeypatch)
    paths["richCore"].chmod(0o620)
    with pytest.raises(full.FullRebuildError, match="rich_core_integrity_mismatch"):
        full._load_production_bundle()


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--adapter-code-sha256", "f" * 64),
        ("--configuration-sha256", "e" * 64),
    ],
)
def test_cli_rejects_caller_supplied_authority_hashes(flag, value):
    with pytest.raises(SystemExit):
        full.parse_args(["--run-id", "run-20260905", flag, value])


def test_structural_duck_adapter_is_rejected(tmp_path):
    config = make_config(tmp_path)

    class Duck:
        contract = full.SUPPORTED_RICH_CORE_CONTRACT

        def open_slot(self, **_kwargs):
            return Manager(FakeSlot(FakeAdapter()))

    with pytest.raises(full.FullRebuildError, match="rich_core_contract_unsupported"):
        full.FullRichRebuildCoordinatorV2(config, adapter=Duck())
