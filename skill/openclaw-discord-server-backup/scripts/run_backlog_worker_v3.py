#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
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
MAX_429_RETRIES = 8

# When load_json recovers from a .bak file, the recovery source is recorded here
# and attached to the final output JSON as `recoveredFrom`.
RECOVERED_SOURCES: list[dict[str, str]] = []


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_tw() -> str:
    return datetime.now(TZ_TAIPEI).date().isoformat()


def snowflake_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def newest_cursor(*values: Any) -> str | None:
    cursors = [str(v) for v in values if v]
    if not cursors:
        return None
    return max(cursors, key=snowflake_int)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # If the main file is corrupt, fall back to the newest parseable .bak.
        # Exit non-zero only when both the main file and every .bak fail.
        baks = sorted(path.parent.glob(path.name + ".bak*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for bak in baks:
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            RECOVERED_SOURCES.append({"path": str(path), "recoveredFrom": str(bak)})
            return data
        raise SystemExit(f"FATAL: {path} failed to parse ({exc}) and no usable .bak was found")


def save_json(path: Path, data: Any) -> None:
    # Atomic write: write to .tmp then os.replace, so a crash mid-write never
    # leaves a half-written JSON file behind.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def acquire_lock(lock_path: Path):
    """Take a cross-job mutex with fcntl.flock; return None when already locked."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def merge_disk_state(state: dict[str, Any], state_path: Path) -> dict[str, Any]:
    """Re-read the on-disk state before saving and merge cursors per entry.

    Cursor fields (`lastWrittenMessageId` / `lastMessageId`) are monotonic: the
    numerically larger snowflake wins, so a concurrent daily-sync job can never
    be rolled back by this worker's older in-memory snapshot. Entries that only
    exist on disk are preserved as-is.
    """
    try:
        disk = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return state
    if not isinstance(disk, dict):
        return state
    disk_entries = disk.get("entries") or {}
    entries = state.setdefault("entries", {})
    for key, disk_entry in disk_entries.items():
        if not isinstance(disk_entry, dict):
            continue
        entry = entries.get(key)
        if entry is None:
            entries[key] = disk_entry
            continue
        for field in ("lastWrittenMessageId", "lastMessageId"):
            merged = newest_cursor(entry.get(field), disk_entry.get(field))
            if merged:
                entry[field] = merged
    return state


def save_state_merged(state: dict[str, Any], state_path: Path) -> None:
    """Merge the on-disk state before every state save (including mid-loop saves).

    This shrinks the lost-update window with concurrent LLM daily-sync jobs: even
    if another job writes between two worker saves, its cursor progress is merged
    back before the next save instead of being overwritten by an older snapshot.
    """
    merge_disk_state(state, state_path)
    save_json(state_path, state)


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
    retries_429 = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            if e.code == 429:
                # Bounded 429 retries: after the cap, raise so the entry takes
                # the normal error path instead of blocking the run forever.
                retries_429 += 1
                if retries_429 > MAX_429_RETRIES:
                    raise RuntimeError(f"Discord 429 rate limit persisted after {MAX_429_RETRIES} retries: {body[:200]}")
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


def normalize_queue_items(queue: dict[str, Any], state: dict[str, Any]) -> None:
    """Collapse duplicate queue items by entryKey and keep cursor monotonic.

    Older duplicate active items can otherwise keep the queue permanently active
    even after a newer duplicate reached caught_up. The merged item follows the
    newest cursor and preserves entry metadata from current state.
    """
    items = queue.get("items") or []
    entries = state.get("entries", {})
    merged: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for item in items:
        key = item.get("entryKey")
        if not key:
            passthrough.append(item)
            continue
        if key not in entries:
            # Orphan item (its state entry no longer exists): mark it retired so
            # it stops counting toward the active queue, but keep it for history.
            if item.get("status") != "retired":
                item["status"] = "retired"
                item["updatedAt"] = now_utc()
            passthrough.append(item)
            continue
        entry = entries.get(key, {})
        item_cursor = newest_cursor(item.get("cursorMessageId"), entry.get("lastWrittenMessageId"), entry.get("lastMessageId"))
        item["cursorMessageId"] = item_cursor
        current = merged.get(key)
        if current is None:
            merged[key] = item
            continue
        current_cursor = newest_cursor(current.get("cursorMessageId"))
        replace = snowflake_int(item_cursor) > snowflake_int(current_cursor)
        if snowflake_int(item_cursor) == snowflake_int(current_cursor):
            # Prefer a completed item on equal cursor, otherwise keep lower priority.
            if item.get("status") == "caught_up" and current.get("status") != "caught_up":
                replace = True
            elif item.get("status") != "caught_up" and current.get("status") == "caught_up":
                replace = False
            else:
                replace = int(item.get("priority") or 50) < int(current.get("priority") or 50)
        chosen = item if replace else current
        other = current if replace else item
        chosen["attempts"] = max(int(chosen.get("attempts") or 0), int(other.get("attempts") or 0))
        chosen["cursorMessageId"] = newest_cursor(chosen.get("cursorMessageId"), other.get("cursorMessageId"))
        if not chosen.get("targetHintMessageId"):
            chosen["targetHintMessageId"] = other.get("targetHintMessageId")
        if not chosen.get("createdAt") or (other.get("createdAt") and other.get("createdAt") < chosen.get("createdAt")):
            chosen["createdAt"] = other.get("createdAt")
        chosen["updatedAt"] = now_utc()
        merged[key] = chosen
    for key, item in merged.items():
        entry = entries.get(key, {})
        if entry:
            item["channelId"] = entry.get("channelId")
            item["relativePath"] = entry.get("relativePath", key)
            item["type"] = entry.get("type")
            item["cursorMessageId"] = newest_cursor(item.get("cursorMessageId"), entry.get("lastWrittenMessageId"), entry.get("lastMessageId"))
    queue["items"] = passthrough + list(merged.values())


def upsert_queue_item(queue: dict[str, Any], key: str, entry: dict[str, Any], status: str, reason: str | None = None, priority: int | None = None) -> dict[str, Any]:
    queue.setdefault("version", 1)
    queue.setdefault("items", [])
    entry_cursor = newest_cursor(entry.get("lastWrittenMessageId"), entry.get("lastMessageId"))
    now = now_utc()
    for item in queue["items"]:
        if item.get("entryKey") == key:
            # Reactivating a caught_up item starts a new catch-up cycle, so reset
            # attempts to 0. Otherwise historical attempts would keep audit
            # warnings permanently triggered (alert fatigue).
            if item.get("status") == "caught_up" and status in ACTIVE:
                item["attempts"] = 0
            # Reactivate stale/caught_up items safely and never let an old queue cursor
            # move work behind the durable state cursor. Re-reading behind state can
            # duplicate raw appends; reading from the newest cursor preserves monotonicity.
            item["channelId"] = entry.get("channelId")
            item["relativePath"] = entry.get("relativePath", key)
            item["type"] = entry.get("type")
            item["cursorMessageId"] = newest_cursor(item.get("cursorMessageId"), entry_cursor)
            item["targetHintMessageId"] = entry.get("lastSeenMessageId") or item.get("targetHintMessageId")
            item["status"] = status
            if reason:
                item["reason"] = reason
            elif not item.get("reason"):
                item["reason"] = entry.get("backlogReason") or "state_partial"
            if priority is not None:
                # Same rule as migrate_state_v3.merge_queue: keep the minimum
                # priority number (smaller = more urgent), never downgrade.
                item["priority"] = min(int(item.get("priority") or 99), int(priority))
            elif item.get("priority") is None:
                item["priority"] = 50
            item["updatedAt"] = now
            return item
    item = {
        "entryKey": key,
        "channelId": entry.get("channelId"),
        "relativePath": entry.get("relativePath", key),
        "type": entry.get("type"),
        "cursorMessageId": entry_cursor,
        "targetHintMessageId": entry.get("lastSeenMessageId"),
        "priority": priority if priority is not None else 50,
        "reason": reason or entry.get("backlogReason") or "state_partial",
        "status": status,
        "attempts": 0,
        "createdAt": now,
        "updatedAt": now,
    }
    queue["items"].append(item)
    return item


def select_candidates(state: dict[str, Any], queue: dict[str, Any], limit: int, run_today: str) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    entries = state.get("entries", {})
    selected: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    seen: set[str] = set()

    def add(key: str, entry: dict[str, Any], item: dict[str, Any]) -> None:
        item["cursorMessageId"] = newest_cursor(item.get("cursorMessageId"), entry.get("lastWrittenMessageId"), entry.get("lastMessageId"))
        selected.append((key, entry, item))
        seen.add(key)

    # 1) Active queue always wins. It represents work already discovered by daily
    # sync/audit/backlog and should not be starved by broad stale probes.
    items = [i for i in queue.get("items", []) if i.get("status", "queued") in ACTIVE]
    items.sort(key=lambda i: (int(i.get("priority") or 50), i.get("createdAt") or "", i.get("relativePath") or ""))
    for item in items:
        key = item.get("entryKey")
        if key in entries and key not in seen:
            add(key, entries[key], item)
        if len(selected) >= limit:
            return selected

    # 2) State-marked incomplete/error entries. Upsert refreshes stale/caught_up
    # queue items and keeps the queue cursor monotonic with state.
    partials: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for key, entry in entries.items():
        if key in seen:
            continue
        if entry.get("syncStatus") in {"partial", "queued", "catching_up", "error"} or entry.get("backlogReason"):
            item = upsert_queue_item(queue, key, entry, "queued", reason=entry.get("backlogReason") or "state_partial", priority=40)
            partials.append((key, entry, item))
    partials.sort(key=lambda t: (int(t[2].get("priority") or 50), t[1].get("lastBackup") or "9999-99-99", t[0]))
    for key, entry, item in partials[: max(0, limit - len(selected))]:
        add(key, entry, item)
    if len(selected) >= limit:
        return selected

    # 3) Safety net for quiet channels/threads that became active again. Daily sync
    # intentionally skips old healthy entries and delegates them to backlog. If the
    # backlog worker does not probe healthy-stale entries, they can stay invisible
    # forever. A zero-message probe marks them checked/today; any messages are
    # appended normally from the durable cursor.
    stale_healthy: list[tuple[str, dict[str, Any]]] = []
    for key, entry in entries.items():
        if key in seen:
            continue
        if entry.get("syncStatus", "healthy") != "healthy" or entry.get("backlogReason"):
            continue
        if not newest_cursor(entry.get("lastWrittenMessageId"), entry.get("lastMessageId")):
            continue
        # Use the same base date as the lastBackup value written on caught_up
        # (args.today), so replayed runs for an older date stay consistent.
        if entry.get("lastBackup") == run_today:
            continue
        stale_healthy.append((key, entry))
    stale_healthy.sort(key=lambda t: (t[1].get("lastBackup") or "0000-00-00", t[0]))
    for key, entry in stale_healthy[: max(0, limit - len(selected))]:
        item = upsert_queue_item(queue, key, entry, "queued", reason="healthy_stale_probe", priority=60)
        add(key, entry, item)
    if len(selected) >= limit:
        return selected

    # 4) Bootstrap entries with no durable cursor. Use a synthetic cursor of "0"
    # so the normal after-cursor loop can establish the first real written cursor.
    # This is bounded by the same worker limits and prevents null-cursor entries
    # from being skipped forever.
    bootstrap: list[tuple[str, dict[str, Any]]] = []
    for key, entry in entries.items():
        if key in seen:
            continue
        if newest_cursor(entry.get("lastWrittenMessageId"), entry.get("lastMessageId")):
            continue
        if not entry.get("channelId"):
            continue
        # Probe an empty channel at most once per day to avoid repeated daily
        # bootstrap probes. The base date matches the lastBackup value written
        # on caught_up (args.today), so replayed runs stay consistent.
        if entry.get("lastBackup") == run_today:
            continue
        bootstrap.append((key, entry))
    bootstrap.sort(key=lambda t: (t[1].get("lastBackup") or "0000-00-00", t[0]))
    for key, entry in bootstrap[: max(0, limit - len(selected))]:
        item = upsert_queue_item(queue, key, entry, "queued", reason="bootstrap_needed", priority=90)
        item["cursorMessageId"] = item.get("cursorMessageId") or "0"
        add(key, entry, item)
    return selected


def mark_queue(queue: dict[str, Any], key: str, **updates: Any) -> None:
    for item in queue.get("items", []):
        if item.get("entryKey") == key:
            item.update(updates)
            item["updatedAt"] = now_utc()


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

    # A missing state file is a configuration error: exit non-zero instead of
    # silently starting from an empty state.
    if not state_path.exists():
        print(f"ERROR: state file not found: {state_path}", file=sys.stderr)
        return 1

    # Cross-job lockfile (same directory as the state file). If another run
    # holds the lock, report skipped and exit cleanly.
    lock_path = state_path.parent / ".channel_backup.lock"
    lock_handle = acquire_lock(lock_path)
    if lock_handle is None:
        print(json.dumps({"skipped": "locked", "lock": str(lock_path)}, ensure_ascii=False))
        return 0

    state = load_json(state_path, {})
    queue = load_json(queue_path, {"version": 1, "items": []})
    token = "" if args.dry_run else load_discord_token(Path(args.openclaw_config), args.token_env)
    run_label = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S %z")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not args.dry_run:
        if state_path.exists(): shutil.copy2(state_path, state_path.with_name(state_path.name + f".bak-backlog-v3-{stamp}"))
        if queue_path.exists(): shutil.copy2(queue_path, queue_path.with_name(queue_path.name + f".bak-backlog-v3-{stamp}"))
        # Rotation: keep only the newest N backups per file to avoid unbounded .bak buildup
        for base in (state_path, queue_path):
            baks = sorted(base.parent.glob(base.name + ".bak*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in baks[5:]:
                old.unlink(missing_ok=True)

    normalize_queue_items(queue, state)
    candidates = select_candidates(state, queue, args.max_entries, args.today)
    report = []
    total_batches = 0

    for key, entry, qitem in candidates:
        if total_batches >= args.max_batches:
            break
        cursor = newest_cursor(qitem.get("cursorMessageId"), entry.get("lastWrittenMessageId"), entry.get("lastMessageId"))
        if not cursor:
            cursor = "0"
            qitem["cursorMessageId"] = cursor
        if args.dry_run:
            report.append({"entry": key, "status": "would_process", "cursor": cursor})
            continue

        entry["syncStatus"] = "catching_up"
        entry["backlogAttempts"] = int(entry.get("backlogAttempts") or 0) + 1
        qitem["status"] = "catching_up"
        qitem["cursorMessageId"] = cursor
        qitem["attempts"] = int(qitem.get("attempts") or 0) + 1
        qitem["updatedAt"] = now_utc()
        save_state_merged(state, state_path); save_json(queue_path, queue)

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
                    mark_queue(queue, key, status="caught_up", cursorMessageId=cursor, attempts=0)
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
                save_state_merged(state, state_path); save_json(queue_path, queue)
                if len(msgs) < args.limit and total_batches < args.max_batches:
                    probe = discord_messages(token, entry["channelId"], after=cursor, limit=1)
                    total_batches += 1
                    if not probe:
                        entry["syncStatus"] = "healthy"
                        entry["backlogReason"] = None
                        entry["lastBackup"] = args.today
                        entry["consecutiveErrors"] = 0
                        mark_queue(queue, key, status="caught_up", cursorMessageId=cursor, attempts=0)
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
            save_state_merged(state, state_path); save_json(queue_path, queue)
            report.append({"entry": key, "status": status, "added": added, "batches": batches, "cursor": cursor, "error": error})

    # Final save also goes through merge-then-save so concurrent daily-sync
    # progress is never overwritten.
    if not args.dry_run:
        state["updatedAt"] = now_utc()
        queue["updatedAt"] = now_utc()
        save_state_merged(state, state_path)
        save_json(queue_path, queue)

    # Built-in audit: list only currently ACTIVE queue items over the attempts
    # threshold. caught_up items were reset to 0 attempts and are excluded, so
    # historical attempts can never keep the warning permanently triggered.
    audit_warnings: list[dict[str, Any]] = []
    for item in queue.get("items", []):
        if item.get("status") not in ACTIVE:
            continue
        if int(item.get("attempts") or 0) > 5:
            audit_warnings.append({"entry": item.get("entryKey"), "kind": "attempts", "attempts": int(item.get("attempts") or 0), "status": item.get("status")})
    for key, entry in (state.get("entries") or {}).items():
        if int(entry.get("consecutiveErrors") or 0) > 3:
            audit_warnings.append({"entry": key, "kind": "consecutiveErrors", "consecutiveErrors": int(entry.get("consecutiveErrors") or 0), "syncStatus": entry.get("syncStatus")})

    active_left = sum(1 for i in queue.get("items", []) if i.get("status") in ACTIVE)
    result = {"ok": True, "today": args.today, "processed": len(report), "totalBatches": total_batches, "activeQueueLeft": active_left, "auditWarnings": audit_warnings, "entries": report}
    if RECOVERED_SOURCES:
        result["recoveredFrom"] = RECOVERED_SOURCES
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
