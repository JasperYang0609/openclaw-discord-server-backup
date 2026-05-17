#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ACTIVE = {"queued", "catching_up", "retry"}
TZ_TAIPEI = timezone(timedelta(hours=8))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_tw() -> str:
    return datetime.now(TZ_TAIPEI).date().isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_discord_token(config_path: Path | None, env_name: str) -> str:
    if os.getenv(env_name):
        return os.environ[env_name]
    if config_path and config_path.exists():
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        token = (((cfg.get("channels") or {}).get("discord") or {}).get("token"))
        if token:
            return token
    raise RuntimeError(f"Discord token not found. Set {env_name} or pass --openclaw-config.")


def discord_messages(token: str, channel_id: str, *, after: str | None, limit: int) -> list[dict[str, Any]]:
    params = {"limit": str(limit)}
    if after:
        params["after"] = str(after)
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bot {token}", "User-Agent": "channel-backup-v3/1.0"})
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            if e.code == 429:
                try:
                    retry = float(json.loads(body).get("retry_after", 1.0))
                except Exception:
                    retry = 1.0
                time.sleep(min(retry + 0.25, 30))
                continue
            raise RuntimeError(f"Discord HTTP {e.code}: {body[:500]}")


def author_name(msg: dict[str, Any]) -> str:
    a = msg.get("author") or {}
    return a.get("global_name") or a.get("username") or a.get("id") or "unknown"


def msg_dt(msg: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))


def msg_day_tw(msg: dict[str, Any]) -> str:
    return msg_dt(msg).astimezone(TZ_TAIPEI).date().isoformat()


def clean_content(text: str) -> str:
    return text if text.strip() else "(無文字內容)"


def fmt_raw(msg: dict[str, Any]) -> str:
    dt = msg_dt(msg).astimezone(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S %z")
    content = clean_content(msg.get("content") or "")
    attachments = msg.get("attachments") or []
    if attachments:
        lines = [f"[附件] {a.get('filename','file')} {a.get('url','')}" for a in attachments]
        content = content + "\n" + "\n".join(lines)
    return f"\n### {dt} — {author_name(msg)} — id:{msg['id']}\n\n{content}\n"


def summarize_batch(msgs: list[dict[str, Any]]) -> dict[str, list[str]]:
    text = "\n".join((m.get("content") or "") for m in msgs)
    topics = []
    patterns = [
        ("程式 / app / 實作討論", r"code|bug|Flutter|FlutterFlow|SQL|API|push|commit|錯誤|修正|功能|頁面"),
        ("備份 / OpenClaw / cron / state", r"backup|cron|OpenClaw|state|queue|session|備份|排程"),
        ("營運 / LINE / 社群 / 品牌", r"LINE|社群|品牌|官方|貼文|客戶|行銷"),
        ("一般對話與進度確認", r"."),
    ]
    for label, pat in patterns:
        if re.search(pat, text, re.I):
            topics.append(label)
        if len(topics) >= 3:
            break
    links = []
    for m in msgs:
        links.extend(re.findall(r"https?://\S+", m.get("content") or ""))
        for a in m.get("attachments") or []:
            links.append(f"{a.get('filename','attachment')}: {a.get('url','')}")
    return {
        "topics": topics or ["一般對話與進度確認"],
        "decisions": ["本批為 backlog worker 追趕寫入；詳細決策以原始訊息與後續人工摘要為準。"],
        "todos": ["若本 entry 仍有 queue active，下一輪 worker 會從最新 cursor 繼續追趕。"],
        "links": links[:12] or ["無"],
    }


def ensure_dirs(root: Path, rel: str) -> Path:
    base = root / rel
    (base / "raw").mkdir(parents=True, exist_ok=True)
    (base / "summary").mkdir(parents=True, exist_ok=True)
    (base / "legacy").mkdir(parents=True, exist_ok=True)
    return base


def append_batch(root: Path, entry: dict[str, Any], msgs: list[dict[str, Any]], run_label: str) -> None:
    rel = entry.get("relativePath")
    if not rel:
        raise RuntimeError("entry missing relativePath")
    base = ensure_dirs(root, rel)
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for msg in sorted(msgs, key=lambda m: int(m["id"])):
        by_day[msg_day_tw(msg)].append(msg)
    for day, day_msgs in by_day.items():
        raw = base / "raw" / f"{day}.md"
        with raw.open("a", encoding="utf-8") as f:
            f.write(f"\n\n---\n## V3 backlog append — {run_label} — {len(day_msgs)} messages\n")
            for msg in day_msgs:
                f.write(fmt_raw(msg))
        s = summarize_batch(day_msgs)
        summary = base / "summary" / f"{day}.md"
        with summary.open("a", encoding="utf-8") as f:
            f.write(f"\n\n---\n## V3 backlog summary — {run_label}\n")
            f.write(f"- 新增訊息數：{len(day_msgs)}\n")
            f.write(f"- ID 範圍：{day_msgs[0]['id']} → {day_msgs[-1]['id']}\n")
            f.write(f"- 狀態：backlog catch-up append\n\n")
            f.write("### 主題綱要\n")
            for x in s["topics"]:
                f.write(f"- {x}\n")
            f.write("\n### 決策\n")
            for x in s["decisions"]:
                f.write(f"- {x}\n")
            f.write("\n### 待辦 / blocker\n")
            for x in s["todos"]:
                f.write(f"- {x}\n")
            f.write("\n### 重要連結 / 檔案\n")
            for x in s["links"]:
                f.write(f"- {x}\n")


def upsert_queue_item(queue: dict[str, Any], key: str, entry: dict[str, Any], status: str, reason: str | None = None) -> dict[str, Any]:
    queue.setdefault("version", 1)
    queue.setdefault("items", [])
    for item in queue["items"]:
        if item.get("entryKey") == key:
            return item
    item = {
        "entryKey": key,
        "channelId": entry.get("channelId"),
        "relativePath": entry.get("relativePath", key),
        "type": entry.get("type"),
        "cursorMessageId": entry.get("lastWrittenMessageId") or entry.get("lastMessageId"),
        "targetHintMessageId": entry.get("lastSeenMessageId"),
        "priority": 50,
        "reason": reason or entry.get("backlogReason") or "state_partial",
        "status": status,
        "attempts": 0,
        "createdAt": now_utc(),
        "updatedAt": now_utc(),
    }
    queue["items"].append(item)
    return item


def select_candidates(state: dict[str, Any], queue: dict[str, Any], limit: int) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    entries = state.get("entries", {})
    selected: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    seen = set()
    items = [i for i in queue.get("items", []) if i.get("status", "queued") in ACTIVE]
    items.sort(key=lambda i: (int(i.get("priority") or 50), i.get("createdAt") or "", i.get("relativePath") or ""))
    for item in items:
        key = item.get("entryKey")
        if key in entries and key not in seen:
            selected.append((key, entries[key], item)); seen.add(key)
        if len(selected) >= limit:
            return selected
    partials = []
    for key, entry in entries.items():
        if key in seen:
            continue
        if entry.get("syncStatus") in {"partial", "queued", "catching_up", "error"} or entry.get("backlogReason"):
            item = upsert_queue_item(queue, key, entry, "queued")
            partials.append((key, entry, item))
    partials.sort(key=lambda t: (int(t[2].get("priority") or 50), t[1].get("lastBackup") or "9999-99-99", t[0]))
    selected.extend(partials[: max(0, limit - len(selected))])
    return selected


def mark_queue(queue: dict[str, Any], key: str, **updates: Any) -> None:
    for item in queue.get("items", []):
        if item.get("entryKey") == key:
            item.update(updates)
            item["updatedAt"] = now_utc()
            return


def main() -> int:
    ap = argparse.ArgumentParser(description="Run V3 Discord backlog worker with durable queue.")
    ap.add_argument("--state", required=True)
    ap.add_argument("--queue", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--today", default=today_tw())
    ap.add_argument("--openclaw-config", default=str(Path.home()/".openclaw/openclaw.json"))
    ap.add_argument("--token-env", default="DISCORD_BOT_TOKEN")
    ap.add_argument("--max-entries", type=int, default=4)
    ap.add_argument("--max-batches", type=int, default=12)
    ap.add_argument("--max-batches-per-entry", type=int, default=5)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state_path = Path(args.state)
    queue_path = Path(args.queue)
    root = Path(args.root)
    state = load_json(state_path, {})
    queue = load_json(queue_path, {"version": 1, "items": []})
    token = "" if args.dry_run else load_discord_token(Path(args.openclaw_config), args.token_env)
    run_label = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S %z")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not args.dry_run:
        if state_path.exists(): shutil.copy2(state_path, state_path.with_name(state_path.name + f".bak-backlog-v3-{stamp}"))
        if queue_path.exists(): shutil.copy2(queue_path, queue_path.with_name(queue_path.name + f".bak-backlog-v3-{stamp}"))

    candidates = select_candidates(state, queue, args.max_entries)
    report = []
    total_batches = 0

    for key, entry, qitem in candidates:
        if total_batches >= args.max_batches:
            break
        cursor = qitem.get("cursorMessageId") or entry.get("lastWrittenMessageId") or entry.get("lastMessageId")
        if not cursor:
            report.append({"entry": key, "status": "skipped_bootstrap_not_implemented", "added": 0})
            continue
        if args.dry_run:
            report.append({"entry": key, "status": "would_process", "cursor": cursor})
            continue

        entry["syncStatus"] = "catching_up"
        entry["backlogAttempts"] = int(entry.get("backlogAttempts") or 0) + 1
        qitem["status"] = "catching_up"
        qitem["attempts"] = int(qitem.get("attempts") or 0) + 1
        qitem["updatedAt"] = now_utc()
        save_json(state_path, state); save_json(queue_path, queue)

        added = 0
        batches = 0
        status = "still_queued"
        error = None
        try:
            while batches < args.max_batches_per_entry and total_batches < args.max_batches:
                msgs = discord_messages(token, entry["channelId"], after=cursor, limit=args.limit)
                total_batches += 1; batches += 1
                if not msgs:
                    entry["syncStatus"] = "healthy"
                    entry["backlogReason"] = None
                    entry["lastBackup"] = args.today
                    entry["consecutiveErrors"] = 0
                    mark_queue(queue, key, status="caught_up", cursorMessageId=cursor)
                    status = "caught_up"
                    break
                msgs = sorted(msgs, key=lambda m: int(m["id"]))
                append_batch(root, entry, msgs, run_label)
                cursor = msgs[-1]["id"]
                added += len(msgs)
                entry["lastWrittenMessageId"] = cursor
                entry["lastMessageId"] = cursor
                entry["lastSuccessfulWriteAt"] = now_utc()
                entry["syncStatus"] = "partial"
                entry["backlogReason"] = entry.get("backlogReason") or "page_limit_reached"
                mark_queue(queue, key, status="queued", cursorMessageId=cursor)
                save_json(state_path, state); save_json(queue_path, queue)
                if len(msgs) < args.limit and total_batches < args.max_batches:
                    probe = discord_messages(token, entry["channelId"], after=cursor, limit=1)
                    total_batches += 1
                    if not probe:
                        entry["syncStatus"] = "healthy"
                        entry["backlogReason"] = None
                        entry["lastBackup"] = args.today
                        entry["consecutiveErrors"] = 0
                        mark_queue(queue, key, status="caught_up", cursorMessageId=cursor)
                        status = "caught_up"
                    else:
                        status = "still_queued"
                    break
        except Exception as exc:
            error = str(exc)
            entry["syncStatus"] = "error"
            entry["consecutiveErrors"] = int(entry.get("consecutiveErrors") or 0) + 1
            mark_queue(queue, key, status="retry", cursorMessageId=cursor)
            status = "error"
        finally:
            state["updatedAt"] = now_utc()
            queue["updatedAt"] = now_utc()
            save_json(state_path, state); save_json(queue_path, queue)
            report.append({"entry": key, "status": status, "added": added, "batches": batches, "cursor": cursor, "error": error})

    active_left = sum(1 for i in queue.get("items", []) if i.get("status") in ACTIVE)
    result = {"ok": True, "today": args.today, "processed": len(report), "totalBatches": total_batches, "activeQueueLeft": active_left, "entries": report}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
