#!/usr/bin/env python3
"""Run one deterministic backup component, keep details local, and write a receipt."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


HERE = Path(__file__).resolve().parent
ROLES = {
    "core-backup", "discovery", "daily-sync-1", "daily-sync-2", "daily-sync-3",
    "caught-up-audit", "backlog",
    "weekly-inventory", "weekly-raw", "workspace-snapshot", "health-report",
}

DAILY_FAILURE_CODES = {
    "rich_archive_contract_invalid",
    "rich_archive_current_invalid",
    "rich_archive_merge_failed",
    "rich_archive_not_initialized",
    "rich_archive_readback_mismatch",
    "rich_archive_unavailable",
    "rich_archive_verification_failed",
}


def load_sibling(name: str):
    path = HERE / name
    spec = importlib.util.spec_from_file_location(f"managed_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


health = load_sibling("backup_health_report.py")


def read_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("config root must be an object")
    return data


def reject_symlink_components(path: Path, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise RuntimeError(f"{label} contains a symlinked path component")


def safe_absolute(value: str | Path, label: str, *, require_directory: bool = False) -> Path:
    path = Path(os.path.abspath(Path(value).expanduser()))
    reject_symlink_components(path, label)
    if require_directory and (not path.is_dir() or path.is_symlink()):
        raise RuntimeError(f"{label} is missing or unsafe")
    return path


def workspace_child(workspace: Path, value: str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace / path
    path = Path(os.path.abspath(path))
    reject_symlink_components(path, label)
    if path != workspace and workspace not in path.parents:
        raise RuntimeError(f"{label} escapes workspace")
    return path


def secure_log(path: Path, content: str) -> None:
    health.secure_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def safe_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")


def bounded_config_int(value: Any, *, default: int, maximum: int, label: str) -> int:
    candidate = default if value is None else value
    if isinstance(candidate, bool) or not isinstance(candidate, int) or not 1 <= candidate <= maximum:
        raise RuntimeError(f"{label} is outside the reviewed bound")
    return candidate


def command_for(
    role: str,
    *,
    workspace: Path,
    config_path: Path,
    config: dict[str, Any],
    backup_root: Path,
    receipt_dir: Path,
    today: str,
) -> list[list[str]]:
    # The interpreter is bound by the cron's reviewed argv. Runtime config is
    # data, never an executable redirect surface.
    python = sys.executable
    state = workspace_child(workspace, str(config.get("statePath") or ""), "state path")
    queue = workspace_child(workspace, str(config.get("queuePath") or ""), "queue path")
    discord_root = safe_absolute(str(config.get("backupRoot") or ""), "Discord backup root")
    if not discord_root.is_absolute() or discord_root.parent != backup_root:
        raise RuntimeError("config backupRoot does not match the managed customer root")
    reports = receipt_dir / "reports"
    inventory = reports / "daily-inventory.json"
    mapping = reports / "inventory-mapping.json"
    openclaw_config = safe_absolute(str(config.get("openclawConfig") or Path.home() / ".openclaw/openclaw.json"), "OpenClaw config")
    base_inventory = [
        python, str(HERE / "audit_discord_inventory_v3.py"),
        "--guild-id", str(config["guildId"]), "--state", str(state),
        "--root", str(discord_root), "--openclaw-config", str(openclaw_config),
        "--archived-page-limit", "100", "--compact",
    ]
    if role == "core-backup":
        latest = backup_root / "核心文件" / "latest"
        snapshot = backup_root / "核心文件" / "snapshots" / today
        tool = str(HERE / "core_workspace_backup.py")
        return [
            [python, tool, "backup", "--workspace", str(workspace), "--backup-root", str(backup_root), "--date", today],
            [python, tool, "verify", "--backup-dir", str(latest)],
            [python, tool, "restore-canary", "--backup-dir", str(latest)],
            [python, tool, "verify", "--backup-dir", str(snapshot)],
            [python, tool, "restore-canary", "--backup-dir", str(snapshot)],
        ]
    if role == "discovery":
        return [[*base_inventory, "--out", str(inventory), "--mapping-ledger-out", str(mapping), "--apply"]]
    if role == "weekly-inventory":
        weekly_dir = receipt_dir / "reports" / "weekly" / today
        return [[*base_inventory, "--out", str(weekly_dir / "inventory.json"), "--mapping-ledger-out", str(weekly_dir / "mapping.json")]]
    if role in {"daily-sync-1", "daily-sync-2", "daily-sync-3"}:
        limits = config.get("limits") if isinstance(config.get("limits"), dict) else {}
        max_entries = bounded_config_int(
            limits.get("dailyEntryLimit"), default=6, maximum=6, label="daily entry limit",
        )
        max_messages = bounded_config_int(
            limits.get("dailyMessageLimitPerEntry"), default=60, maximum=60,
            label="daily message limit",
        )
        freshness_days = bounded_config_int(
            limits.get("dailyFreshnessDays"), default=2, maximum=7,
            label="daily freshness window",
        )
        lookback_limit = bounded_config_int(
            limits.get("dailyLookbackLimit"), default=10, maximum=30,
            label="daily lookback limit",
        )
        return [[
            python, str(HERE / "run_daily_sync_v3.py"),
            "--role", role,
            "--state", str(state),
            "--queue", str(queue),
            "--root", str(discord_root),
            "--inventory", str(inventory),
            "--today", today,
            "--timezone", str(config.get("timezone") or "Asia/Taipei"),
            "--openclaw-config", str(openclaw_config),
            "--max-entries", str(max_entries),
            "--max-write-entries", "4",
            "--page-limit", "30",
            "--max-messages-per-entry", str(max_messages),
            "--max-read-messages", "180",
            "--lookback-limit", str(lookback_limit),
            "--freshness-days", str(freshness_days),
        ]]
    if role == "caught-up-audit":
        return [[
            python, str(HERE / "audit_caught_up_v3.py"), "--state", str(state),
            "--queue", str(queue), "--openclaw-config", str(openclaw_config),
            "--limit", "1", "--requeue", "--out", str(reports / "caught-up-audit.json"),
        ]]
    if role == "backlog":
        return [[
            python, str(HERE / "run_backlog_worker_v3.py"), "--state", str(state),
            "--queue", str(queue), "--root", str(discord_root), "--today", today,
            "--openclaw-config", str(openclaw_config), "--max-entries", "4",
            "--max-batches", "12", "--max-batches-per-entry", "5", "--limit", "100",
        ]]
    if role == "weekly-raw":
        evidence = backup_root / "備份驗證證據" / "weekly-raw" / f"{today}-{safe_stamp()}"
        command = [
            python, str(HERE / "weekly_raw_reconcile_v4.py"), "--state", str(state),
            "--queue", str(queue), "--root", str(discord_root), "--today", today,
            "--evidence-dir", str(evidence), "--openclaw-config", str(openclaw_config),
            "--page-limit", "100", "--max-closeout-passes", "3", "--compact",
        ]
        report_key = ((config.get("weeklyRaw") or {}).get("reportEntryKey"))
        if report_key:
            command.extend(["--report-entry-key", str(report_key)])
        return [command]
    if role == "workspace-snapshot":
        destination = backup_root / "工作區復原資產"
        settings = config.get("workspaceSnapshot") if isinstance(config.get("workspaceSnapshot"), dict) else {}
        includes = settings.get("includes") if isinstance(settings.get("includes"), list) else ["memory", "scripts", "skills", "hooks", "records", "reports"]
        create = [
            python, str(HERE / "backup_workspace_assets.py"), "create",
            "--workspace", str(workspace), "--destination", str(destination),
            "--today", today, "--root-markdown", "--skip-existing", "--apply",
        ]
        for item in includes:
            create.extend(["--include", str(item)])
        snapshot = destination / "snapshots" / today
        return [
            create,
            [python, str(HERE / "backup_workspace_assets.py"), "verify", "--snapshot", str(snapshot)],
            [python, str(HERE / "backup_workspace_assets.py"), "restore-canary", "--snapshot", str(snapshot)],
        ]
    raise RuntimeError(f"unsupported managed role: {role}")


def parse_metrics(text: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            for key in (
                "activeQueueLeft", "activeQueue", "remainingMissing", "appended",
                "finalLiveOnly", "finalLiveErrors", "checked", "writtenEntries",
                "writtenMessages", "refreshedMessages", "mergedMessages",
                "queued", "totalRead",
            ):
                if key in data and isinstance(data[key], (int, float, bool)):
                    metrics[key] = data[key]
        except json.JSONDecodeError:
            pass
    return metrics


def parse_structured(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def is_safe_lock_skip(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    # Only exact machine-readable contracts from our deterministic helpers are
    # treated as a safe lock skip. Human-readable stderr containing words such
    # as "locked" or "already running" must never turn a real failure green.
    if payload.get("skipped") == "locked" and payload.get("ok") in {False, None}:
        return True
    return (
        payload.get("status") == "skipped"
        and payload.get("reason") in {"lock_busy", "backup_lock_busy"}
    )


def is_inventory_gate_skip(payload: dict[str, Any] | None) -> bool:
    return bool(
        payload
        and payload.get("ok") is False
        and payload.get("status") == "skipped"
        and payload.get("reason") == "inventory_blocked"
        and isinstance(payload.get("reasons"), list)
        and payload["reasons"]
        and all(
            reason in {
                "inventory_stale", "inventory_not_ok", "inventory_incomplete",
                "inventory_warnings",
            }
            for reason in payload["reasons"]
        )
    )


def success_summary(role: str, metrics: dict[str, Any]) -> tuple[str, str, list[str]]:
    if role == "core-backup":
        return "ok", "核心文件已完成備份與還原驗證", []
    if role == "discovery":
        return "ok", "頻道與討論串完整清單已更新", []
    if role in {"daily-sync-1", "daily-sync-2", "daily-sync-3"}:
        pending = ["本輪達到安全上限，已交由 backlog 從驗證游標續做"] if int(metrics.get("queued", 0)) else []
        return ("pending" if pending else "ok"), (pending[0] if pending else "本輪日常同步已完成"), pending
    if role == "caught-up-audit":
        pending = ["尚有頻道待追趕"] if int(metrics.get("activeQueue", 0)) else []
        return ("pending" if pending else "ok"), ("尚有頻道待追趕" if pending else "追平稽核通過"), pending
    if role == "backlog":
        pending = ["夜間 backlog 尚未清空，下一輪會續做"] if int(metrics.get("activeQueueLeft", 0)) else []
        return ("pending" if pending else "ok"), (pending[0] if pending else "夜間追趕已完成"), pending
    if role == "weekly-inventory":
        return "ok", "每週完整清單稽核通過", []
    if role == "weekly-raw":
        return "ok", "Raw 完整性與修復證據驗證通過", []
    if role == "workspace-snapshot":
        return "ok", "工作區快照、校驗與還原測試通過", []
    return "ok", "完成", []


def run_role(args: argparse.Namespace) -> int:
    workspace = safe_absolute(args.workspace, "workspace", require_directory=True)
    config_path = workspace_child(workspace, args.config, "config path")
    backup_root = safe_absolute(args.backup_root, "backup root", require_directory=True)
    receipt_dir = workspace_child(workspace, args.receipt_dir, "receipt directory")
    config = read_config(config_path)
    timezone_name = str(config.get("timezone") or "Asia/Taipei")
    today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    commands = command_for(
        args.role, workspace=workspace, config_path=config_path, config=config,
        backup_root=backup_root, receipt_dir=receipt_dir, today=today,
    )
    combined: list[str] = []
    metrics: dict[str, Any] = {}
    structured: list[dict[str, Any]] = []
    returncode = 0
    safe_skip = False
    inventory_skip = False
    failure_payload: dict[str, Any] | None = None
    for command in commands:
        proc = subprocess.run(command, cwd=workspace, text=True, capture_output=True, check=False)
        combined.append("$ " + " ".join(Path(value).name if index in {0, 1} else "[arg]" for index, value in enumerate(command)))
        combined.append(proc.stdout)
        combined.append(proc.stderr)
        metrics.update(parse_metrics(proc.stdout))
        parsed = parse_structured(proc.stdout)
        if parsed is not None:
            structured.append(parsed)
            if is_safe_lock_skip(parsed):
                safe_skip = True
                returncode = proc.returncode
                break
            if is_inventory_gate_skip(parsed):
                inventory_skip = True
                returncode = proc.returncode
                break
        if proc.returncode != 0:
            failure_payload = parsed
            returncode = proc.returncode
            break
    output = "\n".join(combined)
    log = receipt_dir / "logs" / args.role / f"{safe_stamp()}.log"
    secure_log(log, output)
    if safe_skip:
        status = "warning"
        summary = "另一個備份流程仍在執行，本輪已安全略過；資料沒有被覆蓋，下一輪會續做"
        pending = ["等待下一輪自動續做"]
        anomalies = [{
            "code": "backup_lock_busy", "summary": summary,
            "impact": "本輪新訊息備份延後", "dataLoss": "no",
            "repairStatus": "保留原游標並等待下一輪",
        }]
    elif inventory_skip:
        status = "warning"
        summary = "今日完整頻道清單尚未通過驗證，本輪已在讀寫前安全略過"
        pending = ["等待完整清單更新後由下一輪自動續做"]
        anomalies = [{
            "code": "daily_sync_inventory_blocked", "summary": summary,
            "impact": "本輪新訊息備份延後", "dataLoss": "no",
            "repairStatus": "未推進游標，等待驗證後重試",
        }]
    elif returncode == 0:
        status, summary, pending = success_summary(args.role, metrics)
        anomalies: list[dict[str, Any]] = []
    elif (
        args.role in {"daily-sync-1", "daily-sync-2", "daily-sync-3"}
        and failure_payload is not None
        and failure_payload.get("reason") in DAILY_FAILURE_CODES
    ):
        failure_code = str(failure_payload["reason"])
        if failure_code == "rich_archive_not_initialized":
            summary = "Rich Archive 基線尚未建立，日常同步已在讀寫前停止"
            pending = ["先完成並驗證 full rich rebuild，再啟用 deterministic daily sync"]
        else:
            summary = "Rich Archive 驗證失敗，游標已保留"
            pending = ["查看本機受控日誌中的錯誤類型並修復後重試"]
        status = "error"
        anomalies = [{
            "code": failure_code,
            "summary": summary,
            "impact": "本輪新訊息備份延後",
            "dataLoss": "no",
            "repairStatus": "未推進游標，等待 Rich Archive 修復",
        }]
    else:
        status = "error"
        summary = "備份元件執行失敗，已停止後續寫入"
        pending = ["需要查看本機受控日誌並重試"]
        anomalies = [{
            "code": "component_failed", "summary": summary,
            "impact": "本項備份健康狀態無法確認", "dataLoss": "unknown",
            "repairStatus": "失敗告警已啟用，等待重試",
        }]
    producer = (
        "openclaw-discord-server-backup/daily-sync-v1"
        if args.role in {"daily-sync-1", "daily-sync-2", "daily-sync-3"}
        else "openclaw-discord-server-backup/run-managed-component.v1"
    )
    health.write_component(
        receipt_dir, args.role, status, summary, args.declaration_key,
        producer=producer,
        checks=[{
            "key": "command_exit",
            "status": "warning" if safe_skip or inventory_skip else ("ok" if returncode == 0 else "error"),
            "summary": "安全略過，下一輪續做" if safe_skip or inventory_skip else ("命令已完成" if returncode == 0 else "命令失敗"),
        }],
        metrics=metrics, anomalies=anomalies, pending=pending,
    )
    print(f"[{args.role}] {summary}")
    # Safe lock contention is an expected, data-preserving skip. Returning
    # success keeps OpenClaw's failure alert (which excludes skipped runs) from
    # misclassifying it as an execution failure. The warning remains visible in
    # the component receipt and consolidated health report.
    return 0 if safe_skip or inventory_skip else returncode


def verify_topology_and_render(args: argparse.Namespace) -> int:
    workspace = safe_absolute(args.workspace, "workspace", require_directory=True)
    config_path = workspace_child(workspace, args.config, "config path")
    config = read_config(config_path)
    backup_root = safe_absolute(args.backup_root, "backup root", require_directory=True)
    receipt_dir = workspace_child(workspace, args.receipt_dir, "receipt directory")
    timezone_name = str(config.get("timezone") or "Asia/Taipei")
    guild_id = str(config.get("guildId") or "")
    report_to = str(config.get("reportChannel") or "")
    agent = str(config.get("agentId") or "main")
    openclaw_bin = args.openclaw_bin
    manager = HERE / "manage_cron_topology.py"
    configured_discord_root = safe_absolute(str(config.get("backupRoot") or ""), "Discord backup root")
    if not configured_discord_root.is_absolute() or configured_discord_root.parent != backup_root:
        raise RuntimeError("config backupRoot does not match the managed customer root")
    command = [
        sys.executable, str(manager), "verify",
        "--workspace", str(workspace),
        "--skill-dir", str(HERE.parent),
        "--config", str(config_path),
        "--backup-root", str(configured_discord_root),
        "--guild-id", guild_id,
        "--report-to", report_to,
        "--agent", agent,
        "--timezone", timezone_name,
        "--receipt-dir", str(receipt_dir),
        "--openclaw-bin", openclaw_bin,
        "--python-executable", sys.executable,
    ]
    if config.get("accountId"):
        command.extend(["--account-id", str(config["accountId"])])
    cron_settings = config.get("cron") if isinstance(config.get("cron"), dict) else {}
    adoption_value = cron_settings.get("adoptionMap")
    prepared_value = cron_settings.get("preparedAdoptionReceipt")
    if prepared_value and not adoption_value:
        raise RuntimeError("prepared adoption receipt requires its configured adoption map")
    if adoption_value:
        command.extend([
            "--adoption-map",
            str(workspace_child(workspace, str(adoption_value), "adoption map")),
        ])
        if prepared_value:
            command.extend([
                "--prepared-adoption-receipt",
                str(workspace_child(
                    workspace, str(prepared_value), "prepared adoption receipt",
                )),
            ])
    proc = subprocess.run(command, cwd=workspace, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        health.write_component(
            receipt_dir, "cron-topology", "error", "排程或失敗告警與安裝規格不一致",
            f"openclaw-discord-server-backup:{guild_id}:topology:v1",
            producer="openclaw-discord-server-backup/health-topology-verify.v1",
            checks=[{"key": "owned_topology", "status": "error", "summary": "每日拓撲驗證未通過"}],
            anomalies=[{
                "code": "cron_topology_drift", "summary": "排程或失敗告警與安裝規格不一致",
                "impact": "部分自動備份可能未依預期執行", "dataLoss": "unknown",
                "repairStatus": "需要重新執行安全升級或人工確認碰撞任務",
            }],
            pending=["等待排程拓撲修復"],
        )
    qwen_value = ((config.get("health") or {}).get("qwenReceiptPath")) if isinstance(config.get("health"), dict) else None
    report = health.render_report(
        receipt_dir,
        now=datetime.now(ZoneInfo(timezone_name)),
        qwen_receipt=Path(os.path.abspath(Path(qwen_value).expanduser())) if qwen_value else None,
    )
    print(report)
    return 0 if proc.returncode == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one managed Discord backup component.")
    parser.add_argument("--role", required=True, choices=sorted(ROLES))
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--backup-root", required=True)
    parser.add_argument("--receipt-dir", required=True)
    parser.add_argument("--declaration-key", required=True)
    parser.add_argument("--openclaw-bin", required=True)
    args = parser.parse_args()
    try:
        if args.role == "health-report":
            return verify_topology_and_render(args)
        return run_role(args)
    except Exception as exc:
        # Keep diagnostics bounded; the cron failure alert points operators to local logs.
        print(json.dumps({"status": "ERROR", "component": args.role, "category": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
