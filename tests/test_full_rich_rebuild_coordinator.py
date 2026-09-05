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
    return full.FullRebuildConfigV2(
        run_id="run-20260905",
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
        configuration_sha256=HASH_C,
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
            "priorPointerSha256": None,
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

    def open_slot(self, *, archive_root, lock_path):
        assert lock_path == archive_root / ".channel_backup.lock"
        self.calls.append(("open_slot", str(lock_path)))
        adapter = self

        class SlotManager(Manager):
            def __enter__(self):
                adapter.lock_entered = True
                return super().__enter__()

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


def test_finalizer_rejects_177_of_178_even_after_rounds(tmp_path):
    config = make_config(tmp_path, count=178)
    adapter = FakeAdapter()
    adapter.final_entry_count = 177
    with pytest.raises(full.FullRebuildError, match="rich_full_run_incomplete"):
        run(config, adapter)
    assert not adapter.root_published


def test_production_entrypoint_fails_closed_without_exact_adapter_v3(tmp_path):
    config = make_config(tmp_path)
    entries_path = tmp_path / "expected.json"
    entries_path.write_text(
        json.dumps({"entries": [entry.audit_record() for entry in config.expected_entries]}),
        encoding="utf-8",
    )
    args = full.parse_args([
        "--run-id", config.run_id,
        "--archive-root", str(config.archive_root),
        "--state", str(config.state_path),
        "--queue", str(config.queue_path),
        "--baseline-dir", str(config.baseline_dir),
        "--baseline-sha256", config.baseline_sha256,
        "--state-sha256", config.expected_state_sha256,
        "--queue-sha256", config.expected_queue_sha256,
        "--expected-entries", str(entries_path),
        "--expected-entry-count", str(config.expected_entry_count),
        "--expected-entry-set-sha256", config.expected_entry_set_sha256,
        "--guild-id", config.guild_id,
        "--timezone", config.timezone_name,
        "--adapter-code-sha256", config.adapter_code_sha256,
        "--configuration-sha256", config.configuration_sha256,
        "--minimum-free-space-bytes", "0",
    ])
    with pytest.raises(full.FullRebuildError, match="rich_core_contract_pending"):
        full.execute(args)


def test_structural_duck_adapter_is_rejected(tmp_path):
    config = make_config(tmp_path)

    class Duck:
        contract = full.SUPPORTED_RICH_CORE_CONTRACT

        def open_slot(self, **_kwargs):
            return Manager(FakeSlot(FakeAdapter()))

    with pytest.raises(full.FullRebuildError, match="rich_core_contract_unsupported"):
        full.FullRichRebuildCoordinatorV2(config, adapter=Duck())
