from __future__ import annotations

import copy
import importlib.util
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill/openclaw-discord-server-backup"
SCRIPT = SKILL / "scripts/manage_cron_topology.py"
spec = importlib.util.spec_from_file_location("manage_cron_topology", SCRIPT)
manager = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = manager
spec.loader.exec_module(manager)


def render(tmp_path: Path):
    workspace = tmp_path / "workspace"
    backup = tmp_path / "customer" / "Discord資料"
    workspace.mkdir()
    backup.mkdir(parents=True)
    (workspace / "memory").mkdir()
    config = workspace / "memory/config.json"
    config.write_text(json.dumps({
        "guildId": "123456789012345678",
        "backupRoot": str(backup),
        "statePath": "memory/state.json",
        "queuePath": "memory/queue.json",
        "reportChannel": "discord:channel:987654321098765432",
        "timezone": "Asia/Taipei",
    }), encoding="utf-8")
    context = manager.RenderContext(
        workspace=workspace,
        skill_dir=SKILL,
        config_path=config,
        backup_root=backup,
        guild_id="123456789012345678",
        report_to="discord:channel:987654321098765432",
        agent="main",
        timezone_name="Asia/Taipei",
        receipt_dir=workspace / "memory/health",
        python_executable="python3",
    )
    manifest = manager.read_json(SKILL / "manifests/owned-cron.v1.json")
    return manager.render_jobs(manifest, context), context


class FakeClient:
    def __init__(self, jobs=None):
        self.jobs = copy.deepcopy(jobs or [])
        self.events: list[tuple] = []
        self.next_id = 1

    def list_jobs(self):
        return copy.deepcopy(self.jobs)

    def converge_disabled(self, desired):
        match = next((row for row in self.jobs if row.get("declarationKey") == desired["declarationKey"]), None)
        job = copy.deepcopy(desired)
        job["enabled"] = False
        if match is None:
            job["id"] = f"new-{self.next_id}"
            self.next_id += 1
            self.jobs.append(job)
        else:
            job["id"] = match["id"]
            self.jobs[self.jobs.index(match)] = job
        self.events.append(("converge", desired["role"], job["id"]))
        return copy.deepcopy(job)

    def set_enabled(self, job_id, enabled):
        row = next(row for row in self.jobs if str(row["id"]) == str(job_id))
        row["enabled"] = bool(enabled)
        self.events.append(("enabled", str(job_id), bool(enabled)))

    def remove(self, job_id):
        self.jobs = [row for row in self.jobs if str(row.get("id")) != str(job_id)]
        self.events.append(("remove", str(job_id)))

    def restore(self, original):
        current = next((row for row in self.jobs if row.get("declarationKey") == original.get("declarationKey")), None)
        if current is not None:
            self.jobs.remove(current)
        self.jobs.append(copy.deepcopy(original))
        self.events.append(("restore", str(original.get("id"))))
        return copy.deepcopy(original)

    def canary(self, workspace):
        self.events.append(("canary",))

    def daily_lock_canary(self, workspace, lock_path):
        self.events.append(("daily-lock", str(lock_path)))


def test_one_shot_canaries_do_not_use_cron_only_exact_flag():
    # OpenClaw 2026.7.1-2 rejects --exact together with the one-shot --at
    # schedule. The production recurring jobs still use --exact.
    assert '"--exact"' not in inspect.getsource(manager.OpenClawCronClient.canary)


def test_canary_cleanup_accepts_rm_race_only_after_absence_readback(monkeypatch):
    client = manager.OpenClawCronClient("openclaw")
    key = "canary-race"
    inventories = iter([[{"id": "gone", "declarationKey": key}], []])
    monkeypatch.setattr(client, "list_jobs", lambda: next(inventories))
    monkeypatch.setattr(client, "remove", lambda _job_id: (_ for _ in ()).throw(manager.CronManagerError("gone")))
    client.remove_declaration_key(key)


def test_canary_cleanup_rejects_rm_failure_when_declaration_remains(monkeypatch):
    client = manager.OpenClawCronClient("openclaw")
    key = "canary-remains"
    monkeypatch.setattr(client, "list_jobs", lambda: [{"id": "still-here", "declarationKey": key}])
    monkeypatch.setattr(client, "remove", lambda _job_id: (_ for _ in ()).throw(manager.CronManagerError("busy")))
    with pytest.raises(manager.CronManagerError, match="remained"):
        client.remove_declaration_key(key)


class MutateThenFailClient(FakeClient):
    def converge_disabled(self, desired):
        job = copy.deepcopy(desired)
        job.update({"id": "created-before-invalid-readback", "enabled": False})
        self.jobs.append(job)
        self.events.append(("mutated-before-readback", desired["declarationKey"]))
        raise manager.CronManagerError("invalid add readback")


class NoOpRemoveClient(FakeClient):
    def remove(self, job_id):
        self.events.append(("remove-noop", str(job_id)))


class NoOpRestoreClient(FakeClient):
    def restore(self, original):
        self.events.append(("restore-noop", str(original.get("id"))))
        return copy.deepcopy(original)


class NoOpReenableClient(FakeClient):
    def set_enabled(self, job_id, enabled):
        if enabled:
            self.events.append(("enabled-noop", str(job_id), True))
            return
        super().set_enabled(job_id, enabled)


class TamperUnknownOnRemoveClient(FakeClient):
    def remove(self, job_id):
        super().remove(job_id)
        unknown = next((row for row in self.jobs if row.get("id") == "third-party"), None)
        if unknown is not None:
            unknown["description"] = "mutated during rollback"


class QuiescenceReadbackFailureClient(NoOpReenableClient):
    def __init__(self, jobs=None):
        super().__init__(jobs)
        self.list_calls = 0

    def list_jobs(self):
        self.list_calls += 1
        if self.list_calls == 1:
            raise manager.CronManagerError("injected quiescence readback failure")
        return super().list_jobs()


def with_ids(desired):
    rows = []
    for index, job in enumerate(desired):
        row = copy.deepcopy(job)
        row["id"] = f"owned-{index}"
        rows.append(row)
    return rows


def legacy_daily_agent(expected):
    row = copy.deepcopy(expected)
    row["sessionTarget"] = "session:legacy-discord-daily-sync"
    row["agentId"] = "main"
    row["payload"] = {
        "kind": "agentTurn",
        "message": "run prompts/daily-sync-v3.md with check_daily_sync_gate.py",
        "timeoutSeconds": expected["payload"]["timeoutSeconds"],
        "lightContext": True,
    }
    return row


def test_manifest_contract_and_rendered_commands(tmp_path):
    desired, _ = render(tmp_path)
    by_role = {row["role"]: row for row in desired}
    assert len(desired) == 11
    assert by_role["core-backup"]["schedule"]["expr"] == "10 5 * * *"
    assert by_role["backlog"]["schedule"]["expr"] == "10 23,0,1,2,3,4 * * *"
    assert by_role["workspace-snapshot"]["schedule"]["expr"] == "0 7 1 * *"
    assert by_role["health-report"]["schedule"]["expr"] == "5 7 * * *"
    daily = [by_role[f"daily-sync-{index}"] for index in (1, 2, 3)]
    assert {row["sessionTarget"] for row in daily} == {"isolated"}
    for row in daily:
        assert row["payload"]["kind"] == "command"
        assert "run_managed_component.py" in row["payload"]["argv"][1]
        assert row["role"] == row["payload"]["argv"][row["payload"]["argv"].index("--role") + 1]
    assert all(row["failureAlert"]["after"] == 1 and not row["failureAlert"]["includeSkipped"] for row in desired)
    assert [row["role"] for row in desired if row["delivery"]["mode"] == "announce"] == ["health-report"]


@pytest.mark.parametrize("payload", [
    {"jobs": [], "total": 0, "offset": 0, "hasMore": True},
    {"jobs": [], "total": 1, "offset": 0, "hasMore": False},
    {"jobs": [], "total": 0, "offset": 1, "hasMore": False},
    {"jobs": "bad", "total": 0, "offset": 0, "hasMore": False},
    {"jobs": [{"id": "x"}, {"id": "x"}], "total": 2, "offset": 0, "hasMore": False},
    {"jobs": [{"id": "x", "declarationKey": "dup"}, {"id": "y", "declarationKey": "dup"}], "total": 2, "offset": 0, "hasMore": False},
])
def test_inventory_fail_closed(payload):
    with pytest.raises(manager.CronManagerError):
        manager.validate_inventory(payload)


def test_unknown_collision_is_preserved_and_blocks(tmp_path):
    desired, _ = render(tmp_path)
    unknown = {"id": "legacy-x", "payload": {"kind": "command", "argv": ["python3", "/x/run_backlog_worker_v3.py"]}}
    plan = manager.build_plan([unknown], desired)
    assert not plan["ok"]
    assert plan["unknownCollisions"][0]["jobId"] == "legacy-x"


def test_allowlisted_keyed_adoption_requires_exact_id_and_fingerprint(tmp_path):
    desired, context = render(tmp_path)
    legacy = {
        "id": "legacy-monthly", "declarationKey": "workspace-critical-assets-monthly-v1",
        "name": "old", "enabled": True, "schedule": {"kind": "cron", "expr": "0 7 1 * *", "tz": "Asia/Taipei"},
        "sessionTarget": "isolated", "payload": {"kind": "command", "argv": ["python3", "/old/backup_workspace_assets.py"]},
        "delivery": {"mode": "none"},
    }
    adoption = context.workspace / "memory/adoption.json"
    adoption.write_text(json.dumps({
        "schema": "openclaw-cron-adoption-map.v1", "guildId": context.guild_id,
        "entries": [{"role": "workspace-snapshot", "jobId": legacy["id"], "jobSha256": manager.job_fingerprint(legacy)}],
    }), encoding="utf-8")
    adopted = manager.validate_adoption_map(adoption, [legacy], desired, context.guild_id)
    assert adopted["workspace-snapshot"]["id"] == legacy["id"]
    data = json.loads(adoption.read_text())
    data["entries"][0]["jobSha256"] = "0" * 64
    adoption.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(manager.CronManagerError):
        manager.validate_adoption_map(adoption, [legacy], desired, context.guild_id)


def test_daily_adoption_rejects_a_swapped_slot(tmp_path):
    desired, context = render(tmp_path)
    expected = next(row for row in desired if row["role"] == "daily-sync-1")
    legacy = legacy_daily_agent(expected)
    legacy.pop("declarationKey")
    legacy["id"] = "legacy-sync-two"
    legacy["schedule"]["expr"] = "40 5 * * *"
    adoption = context.workspace / "memory/adoption-swapped.json"
    adoption.write_text(json.dumps({
        "schema": "openclaw-cron-adoption-map.v1", "guildId": context.guild_id,
        "entries": [{"role": "daily-sync-1", "jobId": legacy["id"], "jobSha256": manager.job_fingerprint(legacy)}],
    }), encoding="utf-8")
    with pytest.raises(manager.CronManagerError, match="schedule"):
        manager.validate_adoption_map(adoption, [legacy], desired, context.guild_id)


def test_backlog_legacy_agent_turn_can_be_adopted_into_command_role(tmp_path):
    desired, context = render(tmp_path)
    expected = next(row for row in desired if row["role"] == "backlog")
    legacy = copy.deepcopy(expected)
    legacy.pop("declarationKey")
    legacy["id"] = "legacy-backlog-agent"
    legacy["payload"] = {
        "kind": "agentTurn",
        "message": "Run scripts/run_backlog_worker_v3.py with the bounded night worker contract.",
        "timeoutSeconds": 3600,
    }
    adoption = context.workspace / "memory/adoption-backlog.json"
    adoption.write_text(json.dumps({
        "schema": "openclaw-cron-adoption-map.v1", "guildId": context.guild_id,
        "entries": [{"role": "backlog", "jobId": legacy["id"], "jobSha256": manager.job_fingerprint(legacy)}],
    }), encoding="utf-8")
    adopted = manager.validate_adoption_map(adoption, [legacy], desired, context.guild_id)
    assert adopted["backlog"]["payload"]["kind"] == "agentTurn"


def test_non_allowlisted_legacy_kind_is_rejected(tmp_path):
    desired, context = render(tmp_path)
    expected = next(row for row in desired if row["role"] == "weekly-raw")
    legacy = copy.deepcopy(expected)
    legacy.pop("declarationKey")
    legacy["id"] = "legacy-weekly-agent"
    legacy["payload"] = {"kind": "agentTurn", "message": "weekly_raw_reconcile_v4.py"}
    adoption = context.workspace / "memory/adoption-wrong-kind.json"
    adoption.write_text(json.dumps({
        "schema": "openclaw-cron-adoption-map.v1", "guildId": context.guild_id,
        "entries": [{"role": "weekly-raw", "jobId": legacy["id"], "jobSha256": manager.job_fingerprint(legacy)}],
    }), encoding="utf-8")
    with pytest.raises(manager.CronManagerError, match="kind"):
        manager.validate_adoption_map(adoption, [legacy], desired, context.guild_id)


def test_fresh_transaction_enables_only_after_canaries(tmp_path):
    desired, context = render(tmp_path)
    client = FakeClient()
    result = manager.apply_plan(client, [], desired, context.receipt_dir, context.workspace)
    assert result["status"] == "ready"
    assert len(client.jobs) == len(desired)
    assert all(row["enabled"] for row in client.jobs)
    canary_index = client.events.index(("canary",))
    lock_index = next(index for index, event in enumerate(client.events) if event[0] == "daily-lock")
    enable_indices = [index for index, event in enumerate(client.events) if event[0] == "enabled" and event[2] is True]
    assert enable_indices and min(enable_indices) > max(canary_index, lock_index)
    assert manager.verify_receipt(Path(result["transaction"]))


def test_daily_lock_canary_proves_non_overlapping_processes(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    client = manager.OpenClawCronClient("unused-for-local-lock-canary")
    client.daily_lock_canary(workspace, workspace / "memory/.channel_backup.lock")


def test_noop_does_not_mutate_or_run_canary(tmp_path):
    desired, context = render(tmp_path)
    current = with_ids(desired)
    client = FakeClient(current)
    result = manager.apply_plan(client, current, desired, context.receipt_dir, context.workspace)
    assert result["mutations"] == 0
    assert result["transaction"] is None
    assert client.events == []


def test_fault_rolls_back_enabled_state_and_created_jobs(tmp_path):
    desired, context = render(tmp_path)
    current = with_ids(desired[:2])
    client = FakeClient(current)
    with pytest.raises(manager.CronManagerError):
        manager.apply_plan(client, current, desired, context.receipt_dir, context.workspace, fault_after=3)
    assert len(client.jobs) == 2
    assert all(row["enabled"] for row in client.jobs)
    assert {row["declarationKey"] for row in client.jobs} == {row["declarationKey"] for row in current}


def test_failed_reconciliation_detects_noop_created_job_removal(tmp_path):
    desired, context = render(tmp_path)
    client = NoOpRemoveClient()
    with pytest.raises(manager.CronRollbackIncompleteError, match="rollback was incomplete"):
        manager.apply_plan(
            client, [], desired, context.receipt_dir, context.workspace,
            run_canary=False, fault_after=1,
        )
    assert any(row.get("declarationKey") == desired[0]["declarationKey"] for row in client.jobs)


def test_failed_reconciliation_detects_noop_owned_restore(tmp_path):
    desired, context = render(tmp_path)
    current = with_ids(desired[:1])
    current[0]["description"] = "original description"
    client = NoOpRestoreClient(current)
    with pytest.raises(manager.CronRollbackIncompleteError, match="rollback was incomplete"):
        manager.apply_plan(
            client, current, desired, context.receipt_dir, context.workspace,
            run_canary=False, fault_after=2,
        )
    assert client.jobs[0]["description"] != "original description"


def test_failed_reconciliation_detects_noop_adopted_reenable(tmp_path):
    desired, context = render(tmp_path)
    expected = next(row for row in desired if row["role"] == "workspace-snapshot")
    legacy = copy.deepcopy(expected)
    legacy.pop("declarationKey")
    legacy.update({"id": "legacy-adopted", "name": "legacy adopted snapshot", "enabled": True})
    client = NoOpReenableClient([legacy])
    with pytest.raises(manager.CronRollbackIncompleteError, match="rollback was incomplete"):
        manager.apply_plan(
            client, [legacy], desired, context.receipt_dir, context.workspace,
            run_canary=False, fault_after=1, adopted={"workspace-snapshot": legacy},
        )
    assert client.jobs[0]["enabled"] is False


def test_failed_reconciliation_detects_unknown_job_mutation_during_rollback(tmp_path):
    desired, context = render(tmp_path)
    unknown = {
        "id": "third-party",
        "declarationKey": "third-party-unrelated-v1",
        "name": "unrelated",
        "description": "preserve me",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "7 12 * * *", "tz": "Asia/Taipei"},
        "sessionTarget": "isolated",
        "payload": {"kind": "command", "argv": ["/usr/bin/true"]},
        "delivery": {"mode": "none"},
    }
    client = TamperUnknownOnRemoveClient([unknown])
    with pytest.raises(manager.CronRollbackIncompleteError, match="rollback was incomplete"):
        manager.apply_plan(
            client, [unknown], desired, context.receipt_dir, context.workspace,
            run_canary=False, fault_after=1,
        )


def test_receipt_backed_post_commit_rollback_restores_exact_topology(tmp_path):
    desired, context = render(tmp_path)
    original = with_ids(desired[:2])
    # Drift one owned definition so the transaction contains an update as well
    # as newly created declarations.
    original[0]["description"] = "old description"
    client = FakeClient(original)
    result = manager.apply_plan(client, original, desired, context.receipt_dir, context.workspace, run_canary=False)
    assert all(row["enabled"] for row in client.jobs)
    rollback_result = manager.rollback_committed_transaction(client, Path(result["transaction"]))
    assert rollback_result["status"] == "rolled_back_after_commit"
    assert len(client.jobs) == len(original)
    by_key = {row["declarationKey"]: row for row in client.jobs}
    for row in original:
        assert manager.job_contract(by_key[row["declarationKey"]]) == manager.job_contract(row)


def test_committed_rollback_detects_noop_created_job_removal(tmp_path):
    desired, context = render(tmp_path)
    applied_client = FakeClient()
    result = manager.apply_plan(
        applied_client, [], desired, context.receipt_dir, context.workspace, run_canary=False,
    )
    rollback_client = NoOpRemoveClient(applied_client.jobs)
    with pytest.raises(manager.CronRollbackIncompleteError, match="committed rollback was incomplete"):
        manager.rollback_committed_transaction(rollback_client, Path(result["transaction"]))


def test_committed_rollback_detects_noop_owned_restore(tmp_path):
    desired, context = render(tmp_path)
    original = with_ids(desired[:1])
    original[0]["description"] = "original description"
    applied_client = FakeClient(original)
    result = manager.apply_plan(
        applied_client, original, desired, context.receipt_dir, context.workspace, run_canary=False,
    )
    rollback_client = NoOpRestoreClient(applied_client.jobs)
    with pytest.raises(manager.CronRollbackIncompleteError, match="committed rollback was incomplete"):
        manager.rollback_committed_transaction(rollback_client, Path(result["transaction"]))


def test_committed_rollback_detects_noop_adopted_reenable(tmp_path):
    _, context = render(tmp_path)
    legacy = {
        "id": "legacy-adopted",
        "declarationKey": "legacy-snapshot-v1",
        "name": "legacy snapshot",
        "description": "preserve",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 7 1 * *", "tz": "Asia/Taipei"},
        "sessionTarget": "isolated",
        "payload": {"kind": "command", "argv": ["python3", "/legacy/snapshot.py"]},
        "delivery": {"mode": "none"},
    }
    transaction = manager.write_receipt(
        context.receipt_dir,
        plan={"actions": [], "adopted": [{"role": "workspace-snapshot", "jobId": legacy["id"]}]},
        before=[legacy],
        desired=[],
    )
    disabled = copy.deepcopy(legacy)
    disabled["enabled"] = False
    client = NoOpReenableClient([disabled])
    with pytest.raises(manager.CronRollbackIncompleteError, match="committed rollback was incomplete"):
        manager.rollback_committed_transaction(client, transaction)


def test_committed_rollback_preserves_unknown_job_contract(tmp_path):
    desired, context = render(tmp_path)
    original = with_ids(desired[:1])
    original[0]["description"] = "original description"
    unknown = {
        "id": "third-party",
        "declarationKey": "third-party-unrelated-v1",
        "name": "unrelated",
        "description": "preserve me",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "7 12 * * *", "tz": "Asia/Taipei"},
        "sessionTarget": "isolated",
        "payload": {"kind": "command", "argv": ["/usr/bin/true"]},
        "delivery": {"mode": "none"},
    }
    client = FakeClient([*original, unknown])
    result = manager.apply_plan(
        client, client.list_jobs(), desired, context.receipt_dir, context.workspace, run_canary=False,
    )
    manager.rollback_committed_transaction(client, Path(result["transaction"]))
    restored_unknown = next(row for row in client.jobs if row["id"] == "third-party")
    assert manager.job_fingerprint(restored_unknown) == manager.job_fingerprint(unknown)


def test_existing_owned_jobs_are_all_disabled_before_first_mutation(tmp_path):
    desired, context = render(tmp_path)
    current = with_ids(desired[:3])
    current[0]["description"] = "drift"
    client = FakeClient(current)
    manager.apply_plan(client, current, desired, context.receipt_dir, context.workspace, run_canary=False)
    first_converge = next(index for index, event in enumerate(client.events) if event[0] == "converge")
    disabled_before = [event for event in client.events[:first_converge] if event[0] == "enabled" and event[2] is False]
    assert {event[1] for event in disabled_before} == {row["id"] for row in current}


def test_cron_add_uses_isolated_session_target_for_daily_command(tmp_path):
    desired, _ = render(tmp_path)
    row = next(item for item in desired if item["role"] == "daily-sync-1")
    args = manager.cron_add_args(row, disabled=True)
    assert args[args.index("--session") + 1] == row["sessionTarget"]
    assert "--disabled" in args


def test_cron_edit_converts_existing_daily_job_to_command_in_place(tmp_path):
    desired, _ = render(tmp_path)
    row = next(item for item in desired if item["role"] == "daily-sync-1")
    args = manager.cron_edit_args("owned-daily", row, disabled=True)

    assert args[:3] == ["cron", "edit", "owned-daily"]
    assert "--command-argv" in args
    assert "--message" not in args
    assert "--disable" in args
    assert "--declaration-key" not in args


def test_command_post_add_never_sends_unsupported_tools_patch(tmp_path, monkeypatch):
    desired, _ = render(tmp_path)
    row = next(item for item in desired if item["payload"]["kind"] == "command")
    row["payload"]["toolsAllow"] = ["exec"]
    client = manager.OpenClawCronClient("openclaw")
    calls = []
    monkeypatch.setattr(client, "run", lambda args, **_kwargs: calls.append(list(args)))

    client.configure_post_add("command-job", row, preserve_tools=True)

    assert len(calls) == 1
    assert "--clear-tools" not in calls[0]
    assert "--tools" not in calls[0]


def test_agent_post_add_still_clears_legacy_tools_policy(tmp_path, monkeypatch):
    row = {"payload": {"kind": "agentTurn"}}
    client = manager.OpenClawCronClient("openclaw")
    calls = []
    monkeypatch.setattr(client, "run", lambda args, **_kwargs: calls.append(list(args)))

    client.configure_post_add("agent-job", row)

    assert len(calls) == 1
    assert "--clear-tools" in calls[0]


def test_agent_post_add_can_restore_tools_policy(tmp_path, monkeypatch):
    row = {"payload": {"kind": "agentTurn", "toolsAllow": ["message", "exec"]}}
    client = manager.OpenClawCronClient("openclaw")
    calls = []
    monkeypatch.setattr(client, "run", lambda args, **_kwargs: calls.append(list(args)))

    client.configure_post_add("agent-job", row, preserve_tools=True)

    assert len(calls) == 1
    assert calls[0][calls[0].index("--tools") + 1] == "message,exec"
    assert "--clear-tools" not in calls[0]


def test_command_tools_policy_is_rejected_before_any_mutation(tmp_path):
    desired, context = render(tmp_path)
    current = with_ids(desired[:1])
    assert current[0]["payload"]["kind"] == "command"
    current[0]["payload"]["toolsAllow"] = ["exec"]
    client = FakeClient(current)

    with pytest.raises(manager.CronManagerError, match="unrestorable tools policy"):
        manager.apply_plan(
            client, client.list_jobs(), desired, context.receipt_dir,
            context.workspace, run_canary=False,
        )

    assert client.events == []


def test_quiescence_disables_all_owned_before_filesystem_phase_and_restores(tmp_path):
    desired, context = render(tmp_path)
    current = with_ids(desired[:3])
    client = FakeClient(current)
    result = manager.prepare_quiescence(client, current, desired, {}, context.receipt_dir)
    assert result["status"] == "quiescence_prepared"
    assert all(row["enabled"] is False for row in client.jobs)
    transaction = Path(result["transaction"])
    assert manager.verify_receipt(transaction)
    manager.rollback_committed_transaction(client, transaction)
    assert all(row["enabled"] is True for row in client.jobs)


def test_quiescence_refuses_running_owned_job_without_mutation(tmp_path):
    desired, context = render(tmp_path)
    current = with_ids(desired[:1])
    current[0]["state"] = {"runningAtMs": 123}
    client = FakeClient(current)
    with pytest.raises(manager.CronManagerError, match="currently running"):
        manager.prepare_quiescence(client, current, desired, {}, context.receipt_dir)
    assert client.events == []
    assert client.jobs[0]["enabled"] is True


def test_quiescence_failure_detects_noop_reenable(tmp_path):
    desired, context = render(tmp_path)
    current = with_ids(desired[:1])
    client = QuiescenceReadbackFailureClient(current)
    with pytest.raises(manager.CronRollbackIncompleteError, match="rollback was incomplete"):
        manager.prepare_quiescence(client, current, desired, {}, context.receipt_dir)
    assert client.jobs[0]["enabled"] is False


def test_direct_upgrade_refuses_running_owned_job_before_mutation(tmp_path):
    desired, context = render(tmp_path)
    current = with_ids(desired[:1])
    current[0]["description"] = "drift"
    current[0]["running"] = True
    client = FakeClient(current)
    with pytest.raises(manager.CronManagerError, match="currently running"):
        manager.apply_plan(client, current, desired, context.receipt_dir, context.workspace, run_canary=False)
    assert client.events == []


def test_adoption_prepare_apply_and_identical_rerun_is_idempotent(tmp_path):
    desired, context = render(tmp_path)
    expected = next(row for row in desired if row["role"] == "workspace-snapshot")
    legacy = copy.deepcopy(expected)
    legacy.update({"id": "legacy-monthly", "name": "legacy workspace snapshot"})
    legacy["declarationKey"] = "workspace-critical-assets-monthly-v1"
    legacy["payload"]["argv"][1] = "/legacy/backup_workspace_assets.py"
    adoption = context.workspace / "memory/adoption.json"
    adoption.write_text(json.dumps({
        "schema": "openclaw-cron-adoption-map.v1", "guildId": context.guild_id,
        "entries": [{
            "role": "workspace-snapshot", "jobId": legacy["id"],
            "jobSha256": manager.job_fingerprint(legacy),
        }],
    }), encoding="utf-8")
    client = FakeClient([legacy])
    adopted = manager.validate_adoption_map(adoption, client.list_jobs(), desired, context.guild_id)
    prepared = manager.prepare_quiescence(client, client.list_jobs(), desired, adopted, context.receipt_dir)
    prepared_path = Path(prepared["transaction"])
    rebound = manager.validate_prepared_adoption(
        prepared_path, adoption, client.list_jobs(), desired, context.guild_id
    )
    applied = manager.apply_plan(
        client, client.list_jobs(), desired, context.receipt_dir, context.workspace,
        run_canary=False, adopted=rebound,
    )
    assert applied["status"] == "ready"
    assert next(row for row in client.jobs if row["id"] == "legacy-monthly")["enabled"] is False
    client.events.clear()
    rebound = manager.validate_prepared_adoption(
        prepared_path, adoption, client.list_jobs(), desired, context.guild_id
    )
    rerun = manager.apply_plan(
        client, client.list_jobs(), desired, context.receipt_dir, context.workspace,
        run_canary=False, adopted=rebound,
    )
    assert rerun["mutations"] == 0
    assert rerun["transaction"] is None
    assert client.events == []


def test_owned_upgrade_preserves_disabled_adopted_legacy(tmp_path):
    desired, context = render(tmp_path)
    current_owned = with_ids(desired)
    current_owned[0]["description"] = "previous owned contract"
    legacy = copy.deepcopy(next(row for row in desired if row["role"] == "workspace-snapshot"))
    legacy.update({
        "id": "legacy-monthly",
        "name": "legacy workspace snapshot",
        "declarationKey": "workspace-critical-assets-monthly-v1",
        "enabled": False,
    })
    client = FakeClient([*current_owned, legacy])

    result = manager.apply_plan(
        client,
        client.list_jobs(),
        desired,
        context.receipt_dir,
        context.workspace,
        run_canary=False,
        adopted={"workspace-snapshot": legacy},
    )

    assert result["status"] == "ready"
    assert next(row for row in client.jobs if row["id"] == "legacy-monthly")["enabled"] is False
    assert not any(event[:2] == ("enabled", "legacy-monthly") for event in client.events)


def test_owned_upgrade_rejects_enabled_adopted_legacy_before_mutation(tmp_path):
    desired, context = render(tmp_path)
    current_owned = with_ids(desired)
    current_owned[0]["description"] = "previous owned contract"
    legacy = copy.deepcopy(next(row for row in desired if row["role"] == "workspace-snapshot"))
    legacy.update({
        "id": "legacy-monthly",
        "declarationKey": "workspace-critical-assets-monthly-v1",
        "enabled": True,
    })
    client = FakeClient([*current_owned, legacy])

    with pytest.raises(manager.CronManagerError, match="must already be disabled"):
        manager.apply_plan(
            client,
            client.list_jobs(),
            desired,
            context.receipt_dir,
            context.workspace,
            run_canary=False,
            adopted={"workspace-snapshot": legacy},
        )

    assert client.events == []


def test_created_job_is_removed_when_converge_mutates_then_readback_fails(tmp_path):
    desired, context = render(tmp_path)
    client = MutateThenFailClient()
    with pytest.raises(manager.CronManagerError, match="invalid add readback"):
        manager.apply_plan(client, [], desired, context.receipt_dir, context.workspace, run_canary=False)
    assert client.jobs == []


def test_topology_component_failure_rolls_back_created_jobs(tmp_path, monkeypatch):
    desired, context = render(tmp_path)
    client = FakeClient()
    monkeypatch.setattr(
        manager, "write_topology_component",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        manager.apply_plan(client, [], desired, context.receipt_dir, context.workspace, run_canary=False)
    assert client.jobs == []


def test_success_result_write_failure_keeps_receipt_identity_observable(tmp_path, monkeypatch):
    desired, context = render(tmp_path)
    monkeypatch.setattr(manager, "best_effort_result", lambda *_args, **_kwargs: False)
    client = FakeClient()
    result = manager.apply_plan(client, [], desired, context.receipt_dir, context.workspace, run_canary=False)
    assert result["status"] == "ready"
    assert result["resultRecorded"] is False
    assert manager.verify_receipt(Path(result["transaction"]))

    prepared_client = FakeClient(with_ids(desired[:1]))
    prepared = manager.prepare_quiescence(
        prepared_client, prepared_client.list_jobs(), desired, {}, context.receipt_dir
    )
    assert prepared["status"] == "quiescence_prepared"
    assert prepared["resultRecorded"] is False
    assert manager.verify_receipt(Path(prepared["transaction"]))


def test_unrestorable_env_blocks_before_any_mutation(tmp_path):
    desired, context = render(tmp_path)
    current = with_ids(desired[:1])
    current[0]["payload"]["env"] = {"TOKEN": "secret"}
    client = FakeClient(current)
    with pytest.raises(manager.CronManagerError, match="environment"):
        manager.apply_plan(client, current, desired, context.receipt_dir, context.workspace, run_canary=False)
    assert client.events == []


def test_tools_allow_is_restored_exactly_after_failed_upgrade(tmp_path, monkeypatch):
    desired, context = render(tmp_path)
    current = with_ids(desired)
    agent_index = next(index for index, row in enumerate(current) if row["role"] == "daily-sync-1")
    agent = legacy_daily_agent(current[agent_index])
    current[agent_index] = agent
    agent["payload"]["toolsAllow"] = ["message", "exec"]
    client = FakeClient(current)
    monkeypatch.setattr(
        manager, "write_topology_component",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        manager.apply_plan(client, current, desired, context.receipt_dir, context.workspace, run_canary=False)
    restored = next(row for row in client.jobs if row["id"] == agent["id"])
    assert restored["payload"]["toolsAllow"] == ["message", "exec"]


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "permissions", "tamper", "oversize"])
def test_receipt_verification_rejects_unsafe_or_tampered_files(tmp_path, mutation):
    desired, context = render(tmp_path)
    transaction = manager.write_receipt(
        context.receipt_dir,
        plan=manager.build_plan([], desired), before=[], desired=desired,
    )
    receipt = transaction / "receipt.json"
    if mutation == "symlink":
        original = transaction / "receipt.original"
        receipt.rename(original)
        receipt.symlink_to(original)
    elif mutation == "hardlink":
        os.link(receipt, transaction / "receipt.extra-link")
    elif mutation == "permissions":
        receipt.chmod(0o644)
    elif mutation == "tamper":
        receipt.write_bytes(receipt.read_bytes() + b" ")
    else:
        receipt.write_bytes(b"x" * (manager.MAX_RECEIPT_BYTES + 1))
    assert manager.verify_receipt(transaction) is False
