#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PARTIAL_MARKERS = (
    "partial",
    "deferred",
    "部分完成",
    "增量部分完成",
    "剩餘交給 backlog",
    "達單輪上限",
)

BOOTSTRAP_REASON = "bootstrap_needed"
PAGE_LIMIT_REASON = "page_limit_reached"
DEFERRED_REASON = "daily_deferred"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_partial(entry: dict[str, Any]) -> bool:
    text = " ".join(str(entry.get(k) or "") for k in (
        "syncStatus",
        "backlogReason",
        "deferredReason",
        "dailySyncStatus",
        "dailySyncNote",
    ))
    low = text.lower()
    return any(marker.lower() in low for marker in PARTIAL_MARKERS)


def reason_for(entry: dict[str, Any]) -> str | None:
    if not entry.get("lastMessageId") and not entry.get("lastWrittenMessageId"):
        return BOOTSTRAP_REASON
    if is_partial(entry):
        note = " ".join(str(entry.get(k) or "") for k in ("deferredReason", "dailySyncStatus", "dailySyncNote"))
        if "上限" in note or "limit" in note.lower() or "partial" in note.lower():
            return PAGE_LIMIT_REASON
        return DEFERRED_REASON
    return None


def priority_for(reason: str | None, entry: dict[str, Any]) -> int:
    hot = bool(entry.get("hot")) or any(s in str(entry.get("relativePath", "")) for s in ("寫code", "VASO", "每日備份監控"))
    base = {
        PAGE_LIMIT_REASON: 10,
        DEFERRED_REASON: 20,
        BOOTSTRAP_REASON: 60,
        None: 90,
    }.get(reason, 50)
    return max(1, base - 5) if hot else base


def make_queue_item(key: str, entry: dict[str, Any], reason: str, created_at: str) -> dict[str, Any]:
    cursor = entry.get("lastWrittenMessageId") or entry.get("lastMessageId")
    return {
        "entryKey": key,
        "channelId": entry.get("channelId"),
        "relativePath": entry.get("relativePath", key),
        "type": entry.get("type"),
        "cursorMessageId": cursor,
        "targetHintMessageId": entry.get("lastSeenMessageId"),
        "priority": priority_for(reason, entry),
        "reason": reason,
        "status": "queued",
        "attempts": int(entry.get("backlogAttempts") or 0),
        "createdAt": created_at,
        "updatedAt": created_at,
    }


def merge_queue(existing: dict[str, Any], new_items: list[dict[str, Any]], ts: str) -> dict[str, Any]:
    queue = existing if isinstance(existing, dict) else {}
    queue.setdefault("version", 1)
    queue.setdefault("items", [])
    by_key: dict[str, dict[str, Any]] = {}
    for item in queue.get("items", []):
        if item.get("entryKey"):
            by_key[item["entryKey"]] = item
    for item in new_items:
        old = by_key.get(item["entryKey"])
        if old:
            old.update({
                "channelId": item.get("channelId"),
                "relativePath": item.get("relativePath"),
                "type": item.get("type"),
                "cursorMessageId": item.get("cursorMessageId"),
                "targetHintMessageId": item.get("targetHintMessageId"),
                "priority": min(int(old.get("priority") or 99), int(item.get("priority") or 99)),
                "reason": item.get("reason") or old.get("reason"),
                "status": "queued" if old.get("status") in (None, "done", "caught_up") else old.get("status"),
                "updatedAt": ts,
            })
        else:
            by_key[item["entryKey"]] = item
    queue["items"] = sorted(by_key.values(), key=lambda x: (int(x.get("priority") or 99), x.get("createdAt") or "", x.get("relativePath") or ""))
    queue["updatedAt"] = ts
    return queue


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate channel backup state to V3 state + backlog queue.")
    ap.add_argument("--state", required=True)
    ap.add_argument("--queue", required=True)
    ap.add_argument("--backup", action="store_true", help="Create timestamped .bak before writing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state_path = Path(args.state)
    queue_path = Path(args.queue)
    state = load_json(state_path, {})
    if not isinstance(state, dict) or not isinstance(state.get("entries"), dict):
        raise SystemExit(f"Unsupported state shape: {state_path}")

    ts = now_iso()
    new_queue_items: list[dict[str, Any]] = []
    stats = {"entries": 0, "healthy": 0, "queued": 0, "partial": 0, "bootstrap": 0}

    state["version"] = 3
    state["schema"] = "channel-backup-state-v3"
    state["queuePath"] = str(queue_path)
    state.setdefault("cursorPolicy", {
        "sourceOfTruth": "lastWrittenMessageId",
        "compatField": "lastMessageId",
        "completeWhen": "message read after cursor returns 0",
        "neverAdvancePastUnwrittenRaw": True,
    })

    for key, entry in state["entries"].items():
        stats["entries"] += 1
        last = entry.get("lastWrittenMessageId") or entry.get("lastMessageId")
        entry["lastWrittenMessageId"] = last
        entry["lastMessageId"] = last
        entry.setdefault("lastSeenMessageId", None)
        entry.setdefault("lastSuccessfulWriteAt", None)
        entry.setdefault("consecutiveErrors", 0)
        entry.setdefault("backlogAttempts", 0)
        entry.setdefault("stateSchemaVersion", 3)

        reason = reason_for(entry)
        if reason == BOOTSTRAP_REASON:
            entry["syncStatus"] = "queued"
            entry["backlogReason"] = reason
            stats["bootstrap"] += 1
        elif reason:
            entry["syncStatus"] = "partial"
            entry["backlogReason"] = reason
            stats["partial"] += 1
        else:
            if entry.get("syncStatus") in ("partial", "queued", "catching_up"):
                entry["syncStatus"] = "healthy"
            entry["syncStatus"] = entry.get("syncStatus") or "healthy"
            entry.setdefault("backlogReason", None)
            stats["healthy"] += 1

        if entry.get("syncStatus") in ("partial", "queued"):
            stats["queued"] += 1
            new_queue_items.append(make_queue_item(key, entry, entry.get("backlogReason") or DEFERRED_REASON, ts))

    queue = merge_queue(load_json(queue_path, {"version": 1, "items": []}), new_queue_items, ts)
    state["updatedAt"] = ts

    result = {"state": str(state_path), "queue": str(queue_path), "stats": stats, "queueItems": len(queue.get("items", []))}
    if args.dry_run:
        print(json.dumps({"dryRun": True, **result}, ensure_ascii=False, indent=2))
        return 0

    if args.backup and state_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(state_path, state_path.with_name(state_path.name + f".bak-v3-{stamp}"))
        if queue_path.exists():
            shutil.copy2(queue_path, queue_path.with_name(queue_path.name + f".bak-v3-{stamp}"))
    dump_json(state_path, state)
    dump_json(queue_path, queue)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
