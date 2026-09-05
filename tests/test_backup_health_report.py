from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill/openclaw-discord-server-backup/scripts/backup_health_report.py"
spec = importlib.util.spec_from_file_location("backup_health_report", SCRIPT)
health = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(health)


NOW = datetime.fromisoformat("2026-09-04T07:05:00+08:00")


def write_ok(receipt_dir: Path, component: str, *, checked_at: datetime = NOW):
    producer = (
        "openclaw-discord-server-backup/daily-sync-v2" if component.startswith("daily-sync-")
        else "openclaw-discord-server-backup/cron-manager.v1" if component == "cron-topology"
        else "openclaw-discord-server-backup/run-managed-component.v1"
    )
    declared_role = "topology" if component == "cron-topology" else component
    return health.write_component(
        receipt_dir, component, "ok", f"{component} verified",
        f"openclaw-discord-server-backup:123456789012345678:{declared_role}:v1",
        producer=producer,
        checks=[{"key": "gate", "status": "ok", "summary": "passed"}],
        checked_at=checked_at.isoformat(),
    )


def complete_local_receipts(receipt_dir: Path):
    for component in health.DAILY_COMPONENTS:
        write_ok(receipt_dir, component)
    for component in health.WEEKLY_COMPONENTS:
        write_ok(receipt_dir, component, checked_at=NOW - timedelta(days=5))
    for component in health.MONTHLY_COMPONENTS:
        write_ok(receipt_dir, component, checked_at=NOW - timedelta(days=31))


def qwen_payload(*, status="ok", anomalies=None):
    return {
        "schema": health.SCHEMA,
        "producer": "qwen-local",
        "declarationKey": "openclaw-lancedb-knowledge-local-snapshot-v1",
        "component": "qwen-local",
        "status": status,
        "checkedAt": NOW.isoformat(),
        "summary": "索引已同步" if status == "ok" else "索引驗證失敗",
        "checks": [{"key": "snapshot", "status": status, "summary": "checked"}],
        "metrics": {},
        "anomalies": anomalies or [],
        "pending": [],
        "freshness": {"status": "current", "maxAgeSeconds": 129600},
    }


def write_qwen(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    health.atomic_json(path, payload)
    os.chmod(path, 0o600)


def test_healthy_report_is_concise_and_monthly_snapshot_is_current(tmp_path):
    complete_local_receipts(tmp_path)
    report = health.render_report(tmp_path, now=NOW)
    assert "✅ 正常" in report
    assert "快照與還原驗證：快照與還原驗證通過" in report
    assert "未設定本機搜尋索引回報" in report
    for forbidden in ("cursor", "added=", "processed=", "/logs/"):
        assert forbidden not in report


def test_all_three_lock_skips_without_later_audit_are_never_green(tmp_path):
    complete_local_receipts(tmp_path)
    for index in (1, 2, 3):
        health.write_component(
            tmp_path, f"daily-sync-{index}", "warning", "本輪已安全略過",
            f"openclaw-discord-server-backup:123456789012345678:daily-sync-{index}:v1",
            producer="openclaw-discord-server-backup/daily-sync-v2",
            anomalies=[{
                "code": "backup_lock_busy", "summary": "本輪已安全略過",
                "impact": "新訊息備份延後", "dataLoss": "no", "repairStatus": "等待續做",
            }], pending=["等待下一輪"], checked_at=(NOW - timedelta(minutes=20-index)).isoformat(),
        )
    # Audit is older than all skips, so it does not prove recovery.
    write_ok(tmp_path, "caught-up-audit", checked_at=NOW - timedelta(hours=1))
    report = health.render_report(tmp_path, now=NOW)
    assert "⚠️ 需注意" in report
    assert "三次日常同步都因另一個備份流程占用" in report
    assert "目前沒有資料遺失" in report


def test_qwen_failure_uses_string_enum_and_never_prints_python_boolean(tmp_path):
    complete_local_receipts(tmp_path)
    qwen = tmp_path / "qwen.json"
    write_qwen(qwen, qwen_payload(status="error", anomalies=[{
        "code": "snapshot_failed", "summary": "索引快照驗證失敗", "impact": "搜尋可能落後",
        "dataLoss": "yes", "repairStatus": "等待重建",
    }]))
    report = health.render_report(tmp_path, now=NOW, qwen_receipt=qwen)
    assert "❌ 異常" in report
    assert "已確認資料缺漏" in report
    assert "True" not in report and "False" not in report


def test_qwen_boolean_data_loss_is_rejected(tmp_path):
    complete_local_receipts(tmp_path)
    qwen = tmp_path / "qwen.json"
    payload = qwen_payload(status="error", anomalies=[{
        "code": "bad", "summary": "bad", "impact": "bad", "dataLoss": True, "repairStatus": "bad",
    }])
    write_qwen(qwen, payload)
    report = health.render_report(tmp_path, now=NOW, qwen_receipt=qwen)
    assert "回報無法信任" in report
    assert "✅ 正常" not in report
    assert "True" not in report and "False" not in report


@pytest.mark.parametrize("mutation", ["producer", "declaration", "freshness", "permissions", "oversize"])
def test_qwen_receipt_tampering_is_never_green(tmp_path, mutation):
    complete_local_receipts(tmp_path)
    qwen = tmp_path / "qwen.json"
    payload = qwen_payload()
    if mutation == "producer":
        payload["producer"] = "not-qwen"
    elif mutation == "declaration":
        payload["declarationKey"] = "unknown"
    elif mutation == "freshness":
        payload["freshness"] = {"status": "current", "maxAgeSeconds": 1}
    write_qwen(qwen, payload)
    if mutation == "permissions":
        os.chmod(qwen, 0o644)
    elif mutation == "oversize":
        qwen.write_text(json.dumps({**payload, "padding": "x" * 20000}), encoding="utf-8")
        os.chmod(qwen, 0o600)
    report = health.render_report(tmp_path, now=NOW, qwen_receipt=qwen)
    assert "✅ 正常" not in report
    assert "無法信任" in report


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_qwen_receipt_through_symlinked_ancestor_is_never_green(tmp_path):
    complete_local_receipts(tmp_path)
    real = tmp_path / "real/sub"
    qwen = real / "qwen.json"
    write_qwen(qwen, qwen_payload())
    link = tmp_path / "linked"
    link.symlink_to(tmp_path / "real", target_is_directory=True)

    report = health.render_report(tmp_path, now=NOW, qwen_receipt=link / "sub/qwen.json")

    assert "✅ 正常" not in report
    assert "無法信任" in report


def test_malformed_local_receipt_is_not_green(tmp_path):
    complete_local_receipts(tmp_path)
    bad = tmp_path / "components/core-backup.json"
    bad.write_text("{}", encoding="utf-8")
    os.chmod(bad, 0o600)
    report = health.render_report(tmp_path, now=NOW)
    assert "❌ 異常" in report
    assert "驗證回報無法信任" in report


def test_local_producer_or_declaration_tampering_is_not_green(tmp_path):
    complete_local_receipts(tmp_path)
    path = tmp_path / "components/core-backup.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["producer"] = "untrusted"
    health.atomic_json(path, payload)
    report = health.render_report(tmp_path, now=NOW)
    assert "✅ 正常" not in report
    assert "驗證回報無法信任" in report


def test_not_yet_due_baselines_are_n_a_without_false_verification_claim(tmp_path):
    for component in health.DAILY_COMPONENTS:
        write_ok(tmp_path, component)
    for component in (*health.WEEKLY_COMPONENTS, *health.MONTHLY_COMPONENTS):
        health.write_component(
            tmp_path, component, "pending", "尚未到首次排程",
            f"openclaw-discord-server-backup:123456789012345678:{component}:v1",
            producer="openclaw-discord-server-backup/installer.v1",
            checks=[{"key": "first_cycle", "status": "pending", "summary": "not due"}],
            metrics={"baselineNotDue": True}, checked_at=NOW.isoformat(),
        )
    report = health.render_report(tmp_path, now=NOW)
    assert "⚠️ 需注意" in report
    assert "已安裝，尚未到首次驗證" in report
    assert "快照與還原驗證通過" not in report


def test_yesterday_daily_receipts_cannot_make_today_green(tmp_path):
    for component in health.DAILY_COMPONENTS:
        write_ok(tmp_path, component, checked_at=NOW - timedelta(days=1))
    for component in health.WEEKLY_COMPONENTS:
        write_ok(tmp_path, component, checked_at=NOW - timedelta(days=5))
    for component in health.MONTHLY_COMPONENTS:
        write_ok(tmp_path, component, checked_at=NOW - timedelta(days=31))
    report = health.render_report(tmp_path, now=NOW)
    assert "✅ 正常" not in report
    assert "❌ 異常" in report
    assert "尚無法確認是否缺漏" in report


def test_previous_night_backlog_does_not_mask_missing_current_date_runs(tmp_path):
    complete_local_receipts(tmp_path)
    write_ok(tmp_path, "backlog", checked_at=datetime.fromisoformat("2026-09-03T23:10:00+08:00"))
    report = health.render_report(tmp_path, now=NOW)
    assert "✅ 正常" not in report
    assert "驗證回報無法信任" in report
    assert "尚無法確認是否缺漏" in report


def test_yesterday_qwen_receipt_cannot_render_as_synced_today(tmp_path):
    complete_local_receipts(tmp_path)
    qwen = tmp_path / "qwen.json"
    payload = qwen_payload()
    payload["checkedAt"] = (NOW - timedelta(days=1)).isoformat()
    write_qwen(qwen, payload)
    report = health.render_report(tmp_path, now=NOW, qwen_receipt=qwen)
    assert "✅ 正常" not in report
    assert "搜尋索引：已同步" not in report
    assert "本機搜尋索引回報無法信任" in report
