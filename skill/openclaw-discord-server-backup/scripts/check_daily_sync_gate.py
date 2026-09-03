#!/usr/bin/env python3
"""Fail closed when daily sync would overlap another backup job or stale discovery."""
from __future__ import annotations

import argparse
import fcntl
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def checked_date(value: object, timezone_name: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    except (ValueError, TypeError):
        return None


def load_inventory(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("inventory root must be an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the deterministic daily-sync preflight gate.")
    parser.add_argument("--state", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--today", required=True)
    parser.add_argument("--timezone", default="Asia/Taipei")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    state_path = Path(args.state)
    inventory_path = Path(args.inventory)
    reasons: list[str] = []
    inventory: dict[str, object] = {}
    try:
        inventory = load_inventory(inventory_path)
    except (OSError, ValueError, json.JSONDecodeError):
        reasons.append("inventory_unreadable")

    if inventory:
        if inventory.get("ok") is not True:
            reasons.append("inventory_not_ok")
        if checked_date(inventory.get("checkedAt"), args.timezone) != args.today:
            reasons.append("inventory_stale")
        if int(inventory.get("remainingMissing") or 0) != 0:
            reasons.append("inventory_incomplete")
        if inventory.get("warnings"):
            reasons.append("inventory_warnings")

    lock_path = state_path.parent / ".channel_backup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            reasons.append("backup_lock_busy")
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    result = {
        "ok": not reasons,
        "reasons": reasons,
        "inventoryDate": checked_date(inventory.get("checkedAt"), args.timezone),
    }
    if args.compact:
        print(f"[daily-sync-gate] ok={str(result['ok']).lower()} reasons={','.join(reasons) or 'none'}")
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
