from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill/openclaw-discord-server-backup"
SCRIPT = SKILL / "scripts/run_managed_component.py"
spec = importlib.util.spec_from_file_location("run_managed_component", SCRIPT)
runner = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


def make_args(tmp_path: Path, role: str):
    workspace = tmp_path / "workspace"
    customer = tmp_path / "customer"
    (workspace / "memory").mkdir(parents=True)
    customer.mkdir()
    config = {
        "guildId": "123456789012345678",
        "backupRoot": str(customer / "Discord資料"),
        "statePath": "memory/state.json",
        "queuePath": "memory/queue.json",
        "reportChannel": "discord:channel:987654321098765432",
        "agentId": "main",
        "timezone": "Asia/Taipei",
    }
    (workspace / "memory/config.json").write_text(json.dumps(config), encoding="utf-8")
    return argparse.Namespace(
        role=role, workspace=str(workspace), config="memory/config.json",
        backup_root=str(customer), receipt_dir="memory/health",
        declaration_key=f"openclaw-discord-server-backup:123456789012345678:{role}:v1",
        openclaw_bin=sys.executable,
    ), workspace, customer


def test_core_role_runs_backup_and_verify_restore_for_latest_and_snapshot(tmp_path):
    args, workspace, customer = make_args(tmp_path, "core-backup")
    config = runner.read_config(workspace / "memory/config.json")
    commands = runner.command_for(
        "core-backup", workspace=workspace, config_path=workspace / "memory/config.json",
        config=config, backup_root=customer, receipt_dir=workspace / "memory/health",
        today="2026-09-04",
    )
    assert [row[2] for row in commands] == ["backup", "verify", "restore-canary", "verify", "restore-canary"]
    assert str(customer / "核心文件/latest") in commands[1]
    assert str(customer / "核心文件/snapshots/2026-09-04") in commands[3]


def test_structured_lock_skip_with_zero_exit_writes_warning(monkeypatch, tmp_path):
    args, workspace, _ = make_args(tmp_path, "backlog")
    monkeypatch.setattr(runner, "command_for", lambda *a, **kw: [[
        sys.executable, "-c", "import json; print(json.dumps({'skipped':'locked','activeQueueLeft':7}))"
    ]])
    assert runner.run_role(args) == 0
    receipt = json.loads((workspace / "memory/health/components/backlog.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "warning"
    assert receipt["metrics"]["activeQueueLeft"] == 7
    assert receipt["anomalies"][0]["code"] == "backup_lock_busy"
    assert receipt["pending"]


def test_structured_lock_skip_stops_followup_commands(monkeypatch, tmp_path):
    args, workspace, _ = make_args(tmp_path, "core-backup")
    marker = tmp_path / "followup-ran"
    monkeypatch.setattr(runner, "command_for", lambda *a, **kw: [
        [sys.executable, "-c", "import json; print(json.dumps({'skipped':'locked'}))"],
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
    ])

    assert runner.run_role(args) == 0
    assert not marker.exists()
    receipt = json.loads((workspace / "memory/health/components/core-backup.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "warning"


def test_structured_lock_skip_with_nonzero_exit_is_safe_success(monkeypatch, tmp_path):
    args, workspace, _ = make_args(tmp_path, "backlog")
    monkeypatch.setattr(runner, "command_for", lambda *a, **kw: [[
        sys.executable, "-c",
        "import json,sys; print(json.dumps({'ok':False,'skipped':'locked'})); sys.exit(2)",
    ]])
    assert runner.run_role(args) == 0
    receipt = json.loads((workspace / "memory/health/components/backlog.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "warning"
    assert receipt["checks"][0]["status"] == "warning"


def test_lock_words_in_unstructured_failure_do_not_mask_error(monkeypatch, tmp_path):
    args, workspace, _ = make_args(tmp_path, "backlog")
    monkeypatch.setattr(runner, "command_for", lambda *a, **kw: [[
        sys.executable, "-c", "import sys; print('already running locked'); sys.exit(2)",
    ]])
    assert runner.run_role(args) == 2
    receipt = json.loads((workspace / "memory/health/components/backlog.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "error"


def test_daily_core_role_never_misclassifies_rich_runner_error_as_success(monkeypatch, tmp_path):
    args, workspace, _ = make_args(tmp_path, "daily-sync-1")
    monkeypatch.setattr(runner, "command_for", lambda *a, **kw: [[
        sys.executable, "-c",
        "import json,sys; print(json.dumps({'ok':False,'status':'error','reason':'rich_archive_not_initialized'})); sys.exit(2)",
    ]])

    assert runner.run_role(args) == 2
    receipt = json.loads(
        (workspace / "memory/health/components/daily-sync-1.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "error"
    assert receipt["anomalies"][0]["code"] == "component_failed"
    assert receipt["anomalies"][0]["dataLoss"] == "unknown"
    assert receipt["producer"] == "openclaw-discord-server-backup/daily-sync-core-v3"


def test_backlog_metrics_create_pending_receipt(monkeypatch, tmp_path):
    args, workspace, _ = make_args(tmp_path, "backlog")
    monkeypatch.setattr(runner, "command_for", lambda *a, **kw: [[
        sys.executable, "-c", "import json; print(json.dumps({'activeQueueLeft':2}))"
    ]])
    assert runner.run_role(args) == 0
    receipt = json.loads((workspace / "memory/health/components/backlog.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "pending"
    assert receipt["metrics"]["activeQueueLeft"] == 2


def test_runtime_config_cannot_redirect_python_or_openclaw_executables(tmp_path):
    args, workspace, customer = make_args(tmp_path, "backlog")
    config_path = workspace / "memory/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["pythonExecutable"] = "/tmp/untrusted-python"
    config["openclawExecutable"] = "/tmp/untrusted-openclaw"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    commands = runner.command_for(
        "backlog", workspace=workspace, config_path=config_path, config=config,
        backup_root=customer, receipt_dir=workspace / "memory/health", today="2026-09-04",
    )
    flattened = [value for command in commands for value in command]
    assert commands[0][0] == sys.executable
    assert "/tmp/untrusted-python" not in flattened
    assert "/tmp/untrusted-openclaw" not in flattened


def test_daily_role_builds_bounded_deterministic_command(tmp_path):
    args, workspace, customer = make_args(tmp_path, "daily-sync-2")
    config_path = workspace / "memory/config.json"
    config = runner.read_config(config_path)
    commands = runner.command_for(
        args.role, workspace=workspace, config_path=config_path, config=config,
        backup_root=customer, receipt_dir=workspace / "memory/health",
        today="2026-09-05",
    )

    assert len(commands) == 1
    command = commands[0]
    assert command[1].endswith("run_backlog_worker_v3.py")
    assert command[command.index("--max-entries") + 1] == "6"
    assert command[command.index("--max-batches") + 1] == "12"
    assert command[command.index("--max-batches-per-entry") + 1] == "2"
    assert command[command.index("--limit") + 1] == "60"
    assert "--inventory" not in command
    assert "--mapping-ledger" not in command


def test_daily_role_rejects_runtime_bounds_above_reviewed_caps(tmp_path):
    args, workspace, customer = make_args(tmp_path, "daily-sync-1")
    config_path = workspace / "memory/config.json"
    config = runner.read_config(config_path)
    config["limits"] = {"dailyEntryLimit": 7}

    with pytest.raises(RuntimeError, match="outside the reviewed bound"):
        runner.command_for(
            args.role, workspace=workspace, config_path=config_path, config=config,
            backup_root=customer, receipt_dir=workspace / "memory/health",
            today="2026-09-05",
        )


def test_daily_role_writes_v2_health_receipt(monkeypatch, tmp_path):
    args, workspace, _ = make_args(tmp_path, "daily-sync-3")
    monkeypatch.setattr(runner, "command_for", lambda *a, **kw: [[
        sys.executable, "-c", "import json; print(json.dumps({'queued': 0, 'checked': 1}))"
    ]])

    assert runner.run_role(args) == 0
    receipt = json.loads(
        (workspace / "memory/health/components/daily-sync-3.json").read_text(encoding="utf-8")
    )
    assert receipt["producer"] == "openclaw-discord-server-backup/daily-sync-core-v3"
    assert receipt["status"] == "ok"


def test_health_report_runs_topology_verify_before_render(monkeypatch, tmp_path, capsys):
    args, workspace, customer = make_args(tmp_path, "health-report")
    config_path = workspace / "memory/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    adoption = workspace / "memory/adoption.json"
    prepared = workspace / "memory/health/transactions/prepared"
    adoption.write_text("{}\n", encoding="utf-8")
    prepared.mkdir(parents=True)
    config["cron"] = {
        "adoptionMap": "memory/adoption.json",
        "preparedAdoptionReceipt": "memory/health/transactions/prepared",
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    receipt_dir = workspace / "memory/health"
    # All non-topology components exist so topology refresh controls the result.
    for component in [*runner.health.DAILY_COMPONENTS, *runner.health.WEEKLY_COMPONENTS, *runner.health.MONTHLY_COMPONENTS]:
        if component == "cron-topology":
            continue
        producer = (
            "openclaw-discord-server-backup/daily-sync-core-v3" if component.startswith("daily-sync-")
            else "openclaw-discord-server-backup/run-managed-component.v1"
        )
        runner.health.write_component(
            receipt_dir, component, "ok", "verified",
            f"openclaw-discord-server-backup:123456789012345678:{component}:v1",
            producer=producer,
            checks=[{"key": "gate", "status": "ok", "summary": "passed"}],
        )
    calls = []

    class Proc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        runner.health.write_component(
            receipt_dir, "cron-topology", "ok", "排程、序列化與失敗告警驗證通過",
            "openclaw-discord-server-backup:123456789012345678:topology:v1",
            producer="openclaw-discord-server-backup/cron-manager.v1",
            checks=[{"key": "owned", "status": "ok", "summary": "passed"}],
        )
        return Proc()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.verify_topology_and_render(args) == 0
    assert calls and "verify" in calls[0]
    assert calls[0][1].endswith("manage_cron_topology.py")
    assert calls[0][calls[0].index("--adoption-map") + 1] == str(adoption)
    assert calls[0][calls[0].index("--prepared-adoption-receipt") + 1] == str(prepared)
    assert "排程與告警：正常" in capsys.readouterr().out


def test_health_report_rejects_prepared_receipt_without_adoption_map(tmp_path):
    args, workspace, _customer = make_args(tmp_path, "health-report")
    config_path = workspace / "memory/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["cron"] = {
        "preparedAdoptionReceipt": "memory/health/transactions/prepared",
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    try:
        runner.verify_topology_and_render(args)
    except RuntimeError as exc:
        assert "requires its configured adoption map" in str(exc)
    else:
        raise AssertionError("prepared receipt without adoption map must fail closed")


def test_health_topology_verify_preserves_legacy_configured_discord_root(monkeypatch, tmp_path, capsys):
    args, workspace, customer = make_args(tmp_path, "health-report")
    config_path = workspace / "memory/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    legacy = customer / "頻道紀錄"
    config["backupRoot"] = str(legacy)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    receipt_dir = workspace / "memory/health"
    for component in [*runner.health.DAILY_COMPONENTS, *runner.health.WEEKLY_COMPONENTS, *runner.health.MONTHLY_COMPONENTS]:
        if component == "cron-topology":
            continue
        producer = (
            "openclaw-discord-server-backup/daily-sync-core-v3" if component.startswith("daily-sync-")
            else "openclaw-discord-server-backup/run-managed-component.v1"
        )
        runner.health.write_component(
            receipt_dir, component, "ok", "verified",
            f"openclaw-discord-server-backup:123456789012345678:{component}:v1",
            producer=producer, checks=[{"key": "gate", "status": "ok", "summary": "passed"}],
        )
    calls = []

    class Proc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        runner.health.write_component(
            receipt_dir, "cron-topology", "ok", "verified",
            "openclaw-discord-server-backup:123456789012345678:topology:v1",
            producer="openclaw-discord-server-backup/cron-manager.v1",
            checks=[{"key": "owned", "status": "ok", "summary": "passed"}],
        )
        return Proc()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.verify_topology_and_render(args) == 0
    command = calls[0]
    assert command[command.index("--backup-root") + 1] == str(legacy)
    assert "排程與告警：正常" in capsys.readouterr().out
