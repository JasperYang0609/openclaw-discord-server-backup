#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ACTIVE_QUEUE_STATUSES = {"queued", "catching_up", "retry"}
PARTIAL_STATUSES = {"partial", "queued", "catching_up"}


def parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def load_json(path: str | None, default: Any) -> Any:
    if not path:
        return default
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def entry_payload(key: str, entry: dict[str, Any], reason: str, priority: int = 50) -> dict[str, Any]:
    cursor = entry.get("lastWrittenMessageId") or entry.get("lastMessageId")
    return {
        "key": key,
        "type": entry.get("type"),
        "channelId": entry.get("channelId"),
        "relativePath": entry.get("relativePath", key),
        "cursorMessageId": cursor,
        "lastMessageId": cursor,
        "lastWrittenMessageId": cursor,
        "lastBackup": entry.get("lastBackup"),
        "syncStatus": entry.get("syncStatus"),
        "backlogReason": entry.get("backlogReason"),
        "reason": reason,
        "priority": priority,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Select backlog worker candidates from V3 backup state + queue.")
    parser.add_argument("--state", required=True, help="Path to state JSON file")
    parser.add_argument("--queue", help="Path to backlog queue JSON file")
    parser.add_argument("--today", required=True, help="YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=4, help="Maximum candidates to emit")
    parser.add_argument("--stale-days", type=int, default=2, help="Treat entries older than this as stale")
    args = parser.parse_args()

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    entries: dict[str, dict[str, Any]] = state.get("entries", {})
    queue_path = args.queue or state.get("queuePath")
    queue = load_json(queue_path, {"items": []})
    today = date.fromisoformat(args.today)
    cutoff = today - timedelta(days=args.stale_days)

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    queued_items = [i for i in queue.get("items", []) if i.get("status", "queued") in ACTIVE_QUEUE_STATUSES]
    queued_items.sort(key=lambda i: (int(i.get("priority") or 50), i.get("createdAt") or "", i.get("relativePath") or ""))
    for item in queued_items:
        key = item.get("entryKey")
        if not key or key not in entries or key in seen:
            continue
        entry = entries[key]
        payload = entry_payload(key, entry, item.get("reason") or entry.get("backlogReason") or "queued", int(item.get("priority") or 50))
        payload["queueStatus"] = item.get("status", "queued")
        selected.append(payload)
        seen.add(key)
        if len(selected) >= args.limit:
            break

    if len(selected) < args.limit:
        partials: list[dict[str, Any]] = []
        for key, entry in entries.items():
            if key in seen:
                continue
            if entry.get("syncStatus") in PARTIAL_STATUSES or entry.get("backlogReason"):
                partials.append(entry_payload(key, entry, entry.get("backlogReason") or "state_partial", 40))
        partials.sort(key=lambda i: (int(i.get("priority") or 50), i.get("lastBackup") or "9999-99-99", i.get("relativePath") or ""))
        for item in partials[: args.limit - len(selected)]:
            selected.append(item)
            seen.add(item["key"])

    if len(selected) < args.limit:
        stale: list[dict[str, Any]] = []
        for key, entry in entries.items():
            if key in seen:
                continue
            cursor = entry.get("lastWrittenMessageId") or entry.get("lastMessageId")
            last_backup = parse_day(entry.get("lastBackup"))
            if cursor and (last_backup is None or last_backup <= cutoff):
                payload = entry_payload(key, entry, "stale_incremental", 70)
                payload["sortDate"] = last_backup.isoformat() if last_backup else "0000-00-00"
                stale.append(payload)
        stale.sort(key=lambda item: (item["sortDate"], item["relativePath"]))
        for item in stale[: args.limit - len(selected)]:
            item.pop("sortDate", None)
            selected.append(item)
            seen.add(item["key"])

    if len(selected) < args.limit:
        bootstrap: list[dict[str, Any]] = []
        for key, entry in entries.items():
            if key in seen:
                continue
            cursor = entry.get("lastWrittenMessageId") or entry.get("lastMessageId")
            if not cursor:
                bootstrap.append(entry_payload(key, entry, "bootstrap_needed", 90))
        bootstrap.sort(key=lambda item: item["relativePath"])
        selected.extend(bootstrap[: args.limit - len(selected)])

    print(json.dumps({
        "today": args.today,
        "cutoff": cutoff.isoformat(),
        "limit": args.limit,
        "queuePath": queue_path,
        "selected": selected,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
