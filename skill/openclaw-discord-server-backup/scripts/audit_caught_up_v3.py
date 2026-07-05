#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVE_QUEUE = {"queued", "catching_up", "retry"}
MAX_429_RETRIES = 8


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_token(config_path: Path | None, env_name: str) -> str:
    if os.getenv(env_name):
        return os.environ[env_name]
    if config_path and config_path.exists():
        cfg = load_json(config_path)
        token = (((cfg.get("channels") or {}).get("discord") or {}).get("token"))
        if token:
            return token
    raise RuntimeError(f"Discord token not found. Set {env_name} or pass OpenClaw config path.")


def read_after(token: str, channel_id: str, cursor: str | None, limit: int) -> dict[str, Any]:
    qs = {"limit": str(limit)}
    if cursor:
        qs["after"] = str(cursor)
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?{urllib.parse.urlencode(qs)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bot {token}", "User-Agent": "openclaw-discord-backup-audit/1.0"})
    retries_429 = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return {"ok": True, "messages": json.loads(r.read().decode("utf-8"))}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            if e.code == 429:
                # Bounded 429 retries: after the cap, report the entry as an
                # error instead of blocking the audit run forever.
                retries_429 += 1
                if retries_429 > MAX_429_RETRIES:
                    return {"ok": False, "error": f"429 rate limit persisted after {MAX_429_RETRIES} retries: {body[:200]}"}
                try:
                    retry = float(json.loads(body).get("retry_after", 1.0))
                except Exception:
                    retry = 1.0
                time.sleep(min(retry + 0.2, 30))
                continue
            return {"ok": False, "error": f"HTTP {e.code}: {body[:500]}"}
        except Exception as exc:
            return {"ok": False, "error": repr(exc)}


def requeue(state: dict[str, Any], queue: dict[str, Any], live_new: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    queue.setdefault("version", 1)
    queue.setdefault("items", [])
    by_key = {item.get("entryKey"): item for item in queue["items"]}
    for row in live_new:
        key = row["key"]
        entry = state["entries"][key]
        entry["syncStatus"] = "partial"
        entry["backlogReason"] = "live_probe_found_new_messages"
        entry["lastSeenMessageId"] = row.get("nextId")
        entry["updatedAt"] = now
        item = by_key.get(key)
        if not item:
            item = {
                "entryKey": key,
                "channelId": entry.get("channelId"),
                "relativePath": entry.get("relativePath", key),
                "type": entry.get("type"),
                "cursorMessageId": entry.get("lastWrittenMessageId") or entry.get("lastMessageId"),
                "targetHintMessageId": row.get("nextId"),
                "priority": 25,
                "reason": "live_probe_found_new_messages",
                "status": "queued",
                "attempts": 0,
                "createdAt": now,
                "updatedAt": now,
            }
            queue["items"].append(item)
        else:
            item.update({
                "cursorMessageId": entry.get("lastWrittenMessageId") or entry.get("lastMessageId"),
                "targetHintMessageId": row.get("nextId"),
                "reason": "live_probe_found_new_messages",
                "status": "queued",
                "updatedAt": now,
            })
    state["updatedAt"] = now
    queue["updatedAt"] = now


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit V3 backup state by probing every entry after its cursor.")
    ap.add_argument("--state", required=True)
    ap.add_argument("--queue", required=True)
    ap.add_argument("--openclaw-config", default=str(Path.home() / ".openclaw/openclaw.json"))
    ap.add_argument("--token-env", default="DISCORD_BOT_TOKEN")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--requeue", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()

    state_path = Path(args.state)
    queue_path = Path(args.queue)
    state = load_json(state_path)
    queue = load_json(queue_path, {"version": 1, "items": []})
    token = load_token(Path(args.openclaw_config), args.token_env)

    live_new = []
    errors = []
    not_healthy = []
    cursor_mismatch = []
    null_cursor = []
    entries = state.get("entries", {})

    for key, entry in entries.items():
        cursor = entry.get("lastWrittenMessageId") or entry.get("lastMessageId")
        if not cursor:
            null_cursor.append(key)
            continue
        if entry.get("lastWrittenMessageId") and entry.get("lastMessageId") and entry.get("lastWrittenMessageId") != entry.get("lastMessageId"):
            cursor_mismatch.append(key)
        if entry.get("syncStatus") not in (None, "healthy") or entry.get("backlogReason"):
            not_healthy.append({"key": key, "syncStatus": entry.get("syncStatus"), "backlogReason": entry.get("backlogReason")})
        res = read_after(token, entry["channelId"], cursor, args.limit)
        if not res["ok"]:
            errors.append({"key": key, "error": res["error"]})
            continue
        if res["messages"]:
            msg = res["messages"][-1]
            live_new.append({"key": key, "cursor": cursor, "nextId": msg.get("id"), "nextTs": msg.get("timestamp")})

    active_queue = [item for item in queue.get("items", []) if item.get("status") in ACTIVE_QUEUE]
    if args.requeue and live_new:
        requeue(state, queue, live_new)
        save_json(state_path, state)
        save_json(queue_path, queue)

    result = {
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "entries": len(entries),
        "probed": len(entries) - len(null_cursor),
        "activeQueue": len(active_queue),
        "notHealthy": not_healthy,
        "cursorMismatch": cursor_mismatch,
        "nullCursor": null_cursor,
        "liveNewAfterCursor": live_new,
        "errors": errors,
        "requeued": bool(args.requeue and live_new),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
