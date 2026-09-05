#!/usr/bin/env python3
"""Write private component receipts and render one plain-language daily report."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA = "backup-health-component.v1"
ALLOWED_STATUS = {"ok", "warning", "error", "pending"}
ALLOWED_DATA_LOSS = {"no", "yes", "unknown"}
SAFE_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DAILY_COMPONENTS = (
    "core-backup", "discovery", "daily-sync-1", "daily-sync-2", "daily-sync-3",
    "caught-up-audit", "backlog", "cron-topology",
)
WEEKLY_COMPONENTS = ("weekly-inventory", "weekly-raw")
MONTHLY_COMPONENTS = ("workspace-snapshot",)
MAX_RECEIPT_BYTES = 16 * 1024
QWEN_PRODUCER = "qwen-local"
QWEN_DECLARATION_KEYS = {
    "openclaw-lancedb-knowledge-local-incremental-v1",
    "openclaw-lancedb-knowledge-local-initial-v1",
    "openclaw-lancedb-knowledge-local-snapshot-v1",
}
QWEN_MAX_AGE_SECONDS = 129_600
LOCAL_PRODUCERS = {
    **{component: {"openclaw-discord-server-backup/run-managed-component.v1"} for component in (
        "core-backup", "discovery", "caught-up-audit", "backlog",
        "weekly-inventory", "weekly-raw", "workspace-snapshot",
    )},
    **{f"daily-sync-{index}": {"openclaw-discord-server-backup/daily-sync-v1"} for index in (1, 2, 3)},
    "cron-topology": {
        "openclaw-discord-server-backup/cron-manager.v1",
        "openclaw-discord-server-backup/health-topology-verify.v1",
    },
}
for _scheduled in (*WEEKLY_COMPONENTS, *MONTHLY_COMPONENTS):
    LOCAL_PRODUCERS[_scheduled].add("openclaw-discord-server-backup/installer.v1")


class HealthError(RuntimeError):
    pass


def secure_directory(path: Path) -> None:
    path = Path(os.path.abspath(path))
    # Symlink rejection remains end-to-end. Ownership/mode validation is
    # anchored at the closest existing directory and every directory we create;
    # unrelated ancestors above that private anchor are outside this product's
    # trust boundary (for example a sticky temporary test root).
    current_component = Path(path.anchor)
    for part in path.parts[1:]:
        current_component /= part
        if os.path.lexists(current_component) and current_component.is_symlink():
            raise HealthError("receipt path contains a symlink")
    current = path
    missing: list[Path] = []
    while not current.exists():
        missing.append(current)
        if current == current.parent:
            break
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise HealthError("receipt path parent is not a safe directory")
    if current != Path(current.anchor):
        info = current.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise HealthError("existing receipt parent must be current-user owned and not group/world writable")
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    checked = [current, *reversed(missing)]
    for item in checked:
        if item == Path(item.anchor):
            continue
        if item.is_symlink() or not item.is_dir():
            raise HealthError("receipt path contains a symlink or non-directory")
        info = item.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise HealthError("receipt directory must be current-user owned and not group/world writable")
    os.chmod(path, 0o700)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    secure_directory(path.parent)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def validate_component_receipt(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise HealthError("unsupported component receipt schema")
    component = payload.get("component")
    if not isinstance(component, str) or not SAFE_COMPONENT.fullmatch(component):
        raise HealthError("invalid component identity")
    if payload.get("status") not in ALLOWED_STATUS:
        raise HealthError("invalid component status")
    if not isinstance(payload.get("checkedAt"), str):
        raise HealthError("component receipt has no checkedAt")
    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        raise HealthError("component receipt has no summary")
    if not isinstance(payload.get("producer"), str) or not payload["producer"].strip():
        raise HealthError("component receipt has no producer identity")
    if not isinstance(payload.get("declarationKey"), str) or not payload["declarationKey"].strip():
        raise HealthError("component receipt has no declaration identity")
    checks = payload.get("checks")
    anomalies = payload.get("anomalies")
    pending = payload.get("pending")
    metrics = payload.get("metrics")
    if not isinstance(checks, list) or not isinstance(anomalies, list) or not isinstance(pending, list) or not isinstance(metrics, dict):
        raise HealthError("component receipt collections are malformed")
    for check in checks:
        if (
            not isinstance(check, dict)
            or not isinstance(check.get("key"), str)
            or check.get("status") not in ALLOWED_STATUS
            or not isinstance(check.get("summary"), str)
        ):
            raise HealthError("component receipt check is malformed")
    for anomaly in anomalies:
        if not isinstance(anomaly, dict):
            raise HealthError("component receipt anomaly is malformed")
        for key in ("code", "summary", "impact", "repairStatus"):
            if not isinstance(anomaly.get(key), str) or not anomaly[key].strip():
                raise HealthError("component receipt anomaly is malformed")
        # Deliberately reject bools: a cross-repo receipt must use the exact
        # no|yes|unknown wire enum so the human report never prints True/False.
        if not isinstance(anomaly.get("dataLoss"), str) or anomaly["dataLoss"] not in ALLOWED_DATA_LOSS:
            raise HealthError("component receipt dataLoss must be no, yes, or unknown")
    if any(not isinstance(item, str) or not item.strip() for item in pending):
        raise HealthError("component receipt pending list is malformed")
    return payload


def write_component(
    receipt_dir: Path,
    component: str,
    status: str,
    summary: str,
    declaration_key: str,
    *,
    producer: str,
    checks: list[dict[str, str]] | None = None,
    metrics: dict[str, Any] | None = None,
    anomalies: list[dict[str, Any]] | None = None,
    pending: list[str] | None = None,
    checked_at: str | None = None,
) -> Path:
    if not SAFE_COMPONENT.fullmatch(component) or status not in ALLOWED_STATUS:
        raise HealthError("invalid component or status")
    payload = {
        "schema": SCHEMA,
        "producer": producer,
        "declarationKey": declaration_key,
        "component": component,
        "status": status,
        "checkedAt": checked_at or datetime.now().astimezone().isoformat(),
        "summary": summary.strip(),
        "checks": checks or [],
        "metrics": metrics or {},
        "anomalies": anomalies or [],
        "pending": pending or [],
    }
    validate_component_receipt(payload)
    path = receipt_dir / "components" / f"{component}.json"
    atomic_json(path, payload)
    return path


def parse_checked_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HealthError("invalid receipt timestamp") from exc
    if parsed.tzinfo is None:
        raise HealthError("receipt timestamp must include timezone")
    return parsed


def read_receipt_bytes(path: Path) -> bytes:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise HealthError("receipt path contains a symlink")
    path = absolute
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise HealthError("receipt parent is missing or unsafe")
    parent_info = parent.lstat()
    if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o022:
        raise HealthError("receipt parent must be current-user owned and not group/world writable")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HealthError("receipt file is missing or unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise HealthError("receipt must be one regular, unlinked file")
        if before.st_uid != os.getuid():
            raise HealthError("receipt owner is not the current user")
        if stat.S_IMODE(before.st_mode) & 0o077:
            raise HealthError("receipt permissions must be owner-only")
        if before.st_size > MAX_RECEIPT_BYTES:
            raise HealthError("receipt exceeds the 16 KiB safety limit")
        chunks: list[bytes] = []
        remaining = MAX_RECEIPT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_nlink)
        if len(data) > MAX_RECEIPT_BYTES or identity(before) != identity(after):
            raise HealthError("receipt changed while being read")
        return data
    finally:
        os.close(descriptor)


def load_receipt(
    path: Path,
    component: str,
    now: datetime,
    max_age: timedelta,
    *,
    enforce_local_identity: bool = True,
    require_current_local_date: bool = False,
) -> dict[str, Any]:
    if not os.path.lexists(path):
        return {
            "schema": SCHEMA, "producer": "health-renderer", "declarationKey": component,
            "component": component, "status": "pending", "checkedAt": now.isoformat(),
            "summary": "尚未收到本期驗證結果", "checks": [], "metrics": {}, "anomalies": [],
            "pending": ["等待下一次排程完成"],
        }
    try:
        payload = validate_component_receipt(json.loads(read_receipt_bytes(path).decode("utf-8")))
        if payload["component"] != component:
            raise HealthError("receipt component identity mismatch")
        if enforce_local_identity:
            if payload["producer"] not in LOCAL_PRODUCERS.get(component, set()):
                raise HealthError("component receipt producer is not allowlisted")
            declared_role = "topology" if component == "cron-topology" else component
            pattern = rf"^openclaw-discord-server-backup:[0-9]{{6,32}}:{re.escape(declared_role)}:v1$"
            if not re.fullmatch(pattern, payload["declarationKey"]):
                raise HealthError("component receipt declaration does not match its role")
        checked = parse_checked_at(payload["checkedAt"])
        age = now.astimezone(checked.tzinfo) - checked
        if age < timedelta(minutes=-5) or age > max_age:
            raise HealthError("receipt is stale")
        if require_current_local_date and checked.astimezone(now.tzinfo).date() != now.date():
            raise HealthError("receipt is not from the current local backup date")
        return payload
    except (OSError, UnicodeError, json.JSONDecodeError, HealthError) as exc:
        return {
            "schema": SCHEMA, "producer": "health-renderer", "declarationKey": component,
            "component": component, "status": "error", "checkedAt": now.isoformat(),
            "summary": "驗證回報無法信任", "checks": [], "metrics": {},
            "anomalies": [{"code": "receipt_invalid", "summary": str(exc), "impact": "無法確認本項備份健康狀態", "dataLoss": "unknown", "repairStatus": "需要重新執行驗證"}],
            "pending": [],
        }


def load_qwen_receipt(path: Path, now: datetime) -> dict[str, Any]:
    payload = load_receipt(
        path, "qwen-local", now, timedelta(seconds=QWEN_MAX_AGE_SECONDS),
        enforce_local_identity=False,
    )
    if payload.get("producer") == "health-renderer":
        return payload
    try:
        if payload.get("producer") != QWEN_PRODUCER:
            raise HealthError("Qwen receipt producer is not allowlisted")
        if payload.get("declarationKey") not in QWEN_DECLARATION_KEYS:
            raise HealthError("Qwen receipt declaration is not allowlisted")
        freshness = payload.get("freshness")
        if freshness != {"status": "current", "maxAgeSeconds": QWEN_MAX_AGE_SECONDS}:
            raise HealthError("Qwen receipt freshness contract is invalid")
        checked = parse_checked_at(payload["checkedAt"])
        age = now.astimezone(checked.tzinfo) - checked
        if age < timedelta(minutes=-5) or age > timedelta(seconds=QWEN_MAX_AGE_SECONDS):
            raise HealthError("Qwen receipt is stale or from the future")
        if checked.astimezone(now.tzinfo).date() != now.date():
            raise HealthError("Qwen receipt is not from the current local backup date")
        return payload
    except HealthError as exc:
        return {
            "schema": SCHEMA, "producer": "health-renderer", "declarationKey": "qwen-local",
            "component": "qwen-local", "status": "error", "checkedAt": now.isoformat(),
            "summary": "本機搜尋索引回報無法信任", "checks": [], "metrics": {},
            "anomalies": [{"code": "qwen_receipt_invalid", "summary": str(exc), "impact": "無法確認搜尋索引是否同步", "dataLoss": "unknown", "repairStatus": "需要重新執行本機索引驗證"}],
            "pending": [],
        }


def worst_status(receipts: list[dict[str, Any]]) -> str:
    rank = {"ok": 0, "pending": 1, "warning": 2, "error": 3}
    return max((receipt["status"] for receipt in receipts), key=rank.__getitem__, default="pending")


def group_summary(receipts: list[dict[str, Any]], healthy_text: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    status = worst_status(receipts)
    anomalies = [item for receipt in receipts for item in receipt.get("anomalies", []) if isinstance(item, dict)]
    pending = [str(item) for receipt in receipts for item in receipt.get("pending", [])]
    if status == "ok":
        return healthy_text, anomalies, pending
    summaries = []
    for receipt in receipts:
        if receipt["status"] != "ok" and receipt["summary"] not in summaries:
            summaries.append(receipt["summary"])
    return "；".join(summaries[:2]) or "等待驗證", anomalies, pending


def all_daily_sync_locked(receipts: dict[str, dict[str, Any]]) -> bool:
    syncs = [receipts[f"daily-sync-{index}"] for index in (1, 2, 3)]
    if not all(any(item.get("code") == "backup_lock_busy" for item in row.get("anomalies", [])) for row in syncs):
        return False
    latest_skip = max(parse_checked_at(row["checkedAt"]) for row in syncs)
    later = receipts.get("caught-up-audit")
    if later and later.get("status") == "ok" and parse_checked_at(later["checkedAt"]) > latest_skip:
        return False
    return True


def render_report(
    receipt_dir: Path,
    *,
    now: datetime,
    qwen_receipt: Path | None = None,
) -> str:
    receipts = {
        component: load_receipt(
            receipt_dir / "components" / f"{component}.json",
            component,
            now,
            timedelta(hours=30),
            require_current_local_date=True,
        )
        for component in DAILY_COMPONENTS
    }
    weekly = {
        component: load_receipt(receipt_dir / "components" / f"{component}.json", component, now, timedelta(days=8))
        for component in WEEKLY_COMPONENTS
    }
    monthly = {
        component: load_receipt(receipt_dir / "components" / f"{component}.json", component, now, timedelta(days=32))
        for component in MONTHLY_COMPONENTS
    }
    qwen = None
    if qwen_receipt is not None:
        qwen = load_qwen_receipt(qwen_receipt, now)

    core_text, core_anomalies, core_pending = group_summary([receipts["core-backup"]], "完整")
    channel_rows = [receipts[name] for name in ("discovery", "daily-sync-1", "daily-sync-2", "daily-sync-3", "caught-up-audit", "backlog")]
    channel_text, channel_anomalies, channel_pending = group_summary(channel_rows, "全部追平")
    if all_daily_sync_locked(receipts):
        channel_text = "三次日常同步都因另一個備份流程占用而安全略過，尚待後續補跑確認"
        channel_anomalies.append({
            "code": "all_daily_sync_locked", "summary": channel_text,
            "impact": "今天的新訊息可能延後備份", "dataLoss": "no",
            "repairStatus": "下一輪會從原游標續做",
        })
        channel_pending.append("等待後續同步或追平稽核")
    snapshot_rows = [*weekly.values(), *monthly.values()]
    snapshot_text, snapshot_anomalies, snapshot_pending = group_summary(snapshot_rows, "快照與還原驗證通過")
    if snapshot_rows and all(row.get("metrics", {}).get("baselineNotDue") is True for row in snapshot_rows):
        snapshot_text = "已安裝，尚未到首次驗證（不影響目前原始備份）"
    topology_text, topology_anomalies, topology_pending = group_summary([receipts["cron-topology"]], "正常")
    if qwen is None:
        index_text = "未設定本機搜尋索引回報（不影響 Discord 原始備份）"
        index_anomalies: list[dict[str, Any]] = []
        index_pending: list[str] = []
    else:
        index_text, index_anomalies, index_pending = group_summary([qwen], "已同步")

    all_rows = [*receipts.values(), *weekly.values(), *monthly.values(), *([qwen] if qwen else [])]
    overall = worst_status(all_rows)
    anomalies = [*core_anomalies, *channel_anomalies, *index_anomalies, *snapshot_anomalies, *topology_anomalies]
    pending = list(dict.fromkeys([*core_pending, *channel_pending, *index_pending, *snapshot_pending, *topology_pending]))
    if anomalies and overall == "ok":
        overall = "warning"
    icon = "✅ 正常" if overall == "ok" else ("⚠️ 需注意" if overall in {"warning", "pending"} else "❌ 異常")
    lines = [
        f"**備份健康｜{now.date().isoformat()} {icon}**",
        "",
        f"- 核心文件：{core_text}",
        f"- 頻道／討論串：{channel_text}",
        f"- 搜尋索引：{index_text}",
        f"- 快照與還原驗證：{snapshot_text}",
        f"- 排程與告警：{topology_text}",
        f"- 待處理：{'；'.join(pending[:3]) if pending else '無'}",
    ]
    if anomalies:
        lines.extend(["", "異常說明："])
        seen: set[str] = set()
        for anomaly in anomalies:
            summary = str(anomaly.get("summary") or "偵測到異常")
            if summary in seen:
                continue
            seen.add(summary)
            impact = str(anomaly.get("impact") or "影響待確認")
            data_loss = str(anomaly.get("dataLoss") or "unknown")
            data_text = {"no": "目前沒有資料遺失", "yes": "已確認資料缺漏", "unknown": "尚無法確認是否缺漏"}.get(data_loss, data_loss)
            repair = str(anomaly.get("repairStatus") or "待處理")
            lines.append(f"- {summary}；影響：{impact}；{data_text}；處理：{repair}")
            if len(seen) >= 5:
                break
    forbidden = ("cursor", "added=", "processed=", "log path", "/logs/")
    result = "\n".join(lines)
    if any(token.lower() in result.lower() for token in forbidden):
        raise HealthError("human report contains forbidden engineering fields")
    return result


def parse_json_arg(value: str, *, expected: type) -> Any:
    data = json.loads(value)
    if not isinstance(data, expected):
        raise argparse.ArgumentTypeError(f"expected {expected.__name__} JSON")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Write backup health receipts or render the daily human report.")
    sub = parser.add_subparsers(dest="operation", required=True)
    record = sub.add_parser("record")
    record.add_argument("--receipt-dir", required=True)
    record.add_argument("--component", required=True)
    record.add_argument("--status", required=True, choices=sorted(ALLOWED_STATUS))
    record.add_argument("--summary", required=True)
    record.add_argument("--declaration-key", required=True)
    record.add_argument("--producer", default="openclaw-discord-server-backup")
    record.add_argument("--checks-json", default="[]")
    record.add_argument("--metrics-json", default="{}")
    record.add_argument("--anomalies-json", default="[]")
    record.add_argument("--pending-json", default="[]")
    record.add_argument("--checked-at")
    render = sub.add_parser("render")
    render.add_argument("--receipt-dir", required=True)
    render.add_argument("--timezone", default="Asia/Taipei")
    render.add_argument("--qwen-receipt")
    render.add_argument("--now", help="Test-only ISO timestamp; defaults to current time")
    args = parser.parse_args()
    try:
        if args.operation == "record":
            path = write_component(
                Path(args.receipt_dir).expanduser().resolve(), args.component, args.status,
                args.summary, args.declaration_key, producer=args.producer,
                checks=parse_json_arg(args.checks_json, expected=list),
                metrics=parse_json_arg(args.metrics_json, expected=dict),
                anomalies=parse_json_arg(args.anomalies_json, expected=list),
                pending=parse_json_arg(args.pending_json, expected=list),
                checked_at=args.checked_at,
            )
            print(json.dumps({"ok": True, "component": args.component, "receipt": str(path)}, ensure_ascii=False))
            return 0
        try:
            zone = ZoneInfo(args.timezone)
        except ZoneInfoNotFoundError as exc:
            raise HealthError("unknown timezone") from exc
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(zone)
        if now.tzinfo is None:
            now = now.replace(tzinfo=zone)
        print(render_report(
            Path(args.receipt_dir).expanduser().resolve(), now=now.astimezone(zone),
            qwen_receipt=Path(os.path.abspath(Path(args.qwen_receipt).expanduser())) if args.qwen_receipt else None,
        ))
        return 0
    except (HealthError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
