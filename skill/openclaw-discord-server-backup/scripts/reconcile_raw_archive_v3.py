#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKER_PATH = Path(__file__).with_name("run_backlog_worker_v3.py")
WORKER_SPEC = importlib.util.spec_from_file_location("reconcile_worker_v3", WORKER_PATH)
if WORKER_SPEC is None or WORKER_SPEC.loader is None:
    raise RuntimeError(f"Unable to load worker module: {WORKER_PATH}")
worker = importlib.util.module_from_spec(WORKER_SPEC)
WORKER_SPEC.loader.exec_module(worker)


ACTIVE_QUEUE = {"queued", "catching_up", "retry"}
HEADER_ID_PATTERNS = (
    re.compile(r"\bid:(\d{15,20})\b"),
    re.compile(r"^#{1,6} .*?(?:\||｜)\s*(\d{15,20})\s*$"),
    re.compile(r"^#{1,6} .*?\((\d{15,20})\)\s*$"),
    re.compile(r"(?:message[_ ]?id|訊息\s*ID)\s*[:：=]\s*(\d{15,20})", re.I),
)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-reconcile")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def archive_message_ids(raw_dir: Path) -> tuple[Counter[str], list[Path]]:
    counts: Counter[str] = Counter()
    files = sorted(raw_dir.glob("*.md")) if raw_dir.is_dir() else []
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            for pattern in HEADER_ID_PATTERNS:
                match = pattern.search(stripped)
                if match:
                    counts[match.group(1)] += 1
                    break
    return counts, files


def newest_id(*values: str | None) -> str | None:
    valid = [str(value) for value in values if value and str(value).isdigit()]
    return max(valid, key=int) if valid else None


def mark_queue_caught_up(queue: dict[str, Any], key: str, cursor: str | None, now: str) -> None:
    for item in queue.get("items") or []:
        if item.get("entryKey") != key:
            continue
        item.update({
            "status": "caught_up",
            "cursorMessageId": cursor,
            "attempts": 0,
            "updatedAt": now,
        })


def acquire_lock(state_path: Path):
    lock_path = state_path.parent / ".channel_backup.lock"
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def fetch_all_messages(token: str, channel_id: str, page_limit: int) -> list[dict[str, Any]]:
    cursor = "0"
    messages: list[dict[str, Any]] = []
    seen: set[str] = set()
    while True:
        page = worker.discord_messages(token, channel_id, after=cursor, limit=page_limit)
        if not page:
            break
        ordered = sorted(page, key=lambda message: int(message["id"]))
        fresh = [message for message in ordered if message["id"] not in seen]
        if not fresh:
            raise RuntimeError(f"Discord pagination did not advance after cursor {cursor}")
        messages.extend(fresh)
        seen.update(message["id"] for message in fresh)
        next_cursor = fresh[-1]["id"]
        if int(next_cursor) <= int(cursor):
            raise RuntimeError(f"Discord pagination cursor moved backward: {cursor} -> {next_cursor}")
        cursor = next_cursor
    return messages


def local_row(key: str, entry: dict[str, Any], root: Path) -> dict[str, Any]:
    relative_path = entry.get("relativePath") or key
    raw_dir = root / relative_path / "raw"
    ids, files = archive_message_ids(raw_dir)
    cursor = newest_id(entry.get("lastWrittenMessageId"), entry.get("lastMessageId"))
    issues: list[str] = []
    if not files:
        issues.append("no_raw_md")
    elif not ids:
        issues.append("no_verifiable_message_ids")
    if cursor and cursor not in ids:
        issues.append("state_cursor_not_in_raw")
    if not cursor:
        issues.append("null_cursor")
    duplicate_ids = sum(count - 1 for count in ids.values() if count > 1)
    if duplicate_ids:
        issues.append("duplicate_raw_message_ids")
    return {
        "key": key,
        "relativePath": relative_path,
        "channelId": entry.get("channelId"),
        "stateCursor": cursor,
        "rawFiles": len(files),
        "rawBytes": sum(path.stat().st_size for path in files),
        "verifiableRawIds": len(ids),
        "duplicateRawIds": duplicate_ids,
        "issues": issues,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    issue_counts = Counter(issue for row in rows for issue in row.get("issues") or [])
    return {
        "entries": len(rows),
        "entriesWithIssues": sum(bool(row.get("issues")) for row in rows),
        "issueCounts": dict(sorted(issue_counts.items())),
        "liveMessages": sum(int(row.get("liveMessages") or 0) for row in rows),
        "missingBeforeRepair": sum(int(row.get("missingBeforeRepair") or 0) for row in rows),
        "appended": sum(int(row.get("appended") or 0) for row in rows),
        "liveErrors": sum(bool(row.get("liveError")) for row in rows),
        "unverifiableEmptyLive": sum(bool(row.get("unverifiableEmptyLive")) for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit every state entry against raw Markdown and optionally rebuild missing Discord history."
    )
    parser.add_argument("--state", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--today", required=True)
    parser.add_argument("--openclaw-config", default=str(Path.home() / ".openclaw/openclaw.json"))
    parser.add_argument("--token-env", default="DISCORD_BOT_TOKEN")
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--compact", action="store_true", help="Print one monitoring-friendly summary line.")
    args = parser.parse_args()

    if args.apply and args.local_only:
        parser.error("--apply cannot be combined with --local-only")
    if not 1 <= args.page_limit <= 100:
        parser.error("--page-limit must be between 1 and 100")

    state_path = Path(args.state)
    queue_path = Path(args.queue)
    root = Path(args.root)
    state = load_json(state_path)
    queue = load_json(queue_path, {"version": 1, "items": []})
    entries = state.get("entries") or {}
    selected = [(key, entry) for key, entry in entries.items() if not args.only or key in args.only]
    unknown = sorted(set(args.only) - set(entries))
    if unknown:
        raise SystemExit(f"Unknown entry keys: {unknown}")

    lock_handle = None
    token = ""
    if not args.local_only:
        token = worker.load_discord_token(Path(args.openclaw_config), args.token_env)
        if args.apply:
            lock_handle = acquire_lock(state_path)
            if lock_handle is None:
                print(json.dumps({"ok": False, "skipped": "locked"}, ensure_ascii=False))
                return 2

    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(state_path, state_path.with_name(state_path.name + f".bak-reconcile-{stamp}"))
        if queue_path.exists():
            shutil.copy2(queue_path, queue_path.with_name(queue_path.name + f".bak-reconcile-{stamp}"))

    rows: list[dict[str, Any]] = []
    for key, entry in selected:
        row = local_row(key, entry, root)
        if args.local_only:
            rows.append(row)
            continue
        if not entry.get("channelId"):
            row["liveError"] = "missing_channel_id"
            rows.append(row)
            continue
        try:
            messages = fetch_all_messages(token, str(entry["channelId"]), args.page_limit)
            live_ids = {message["id"] for message in messages}
            raw_ids, _ = archive_message_ids(root / row["relativePath"] / "raw")
            missing = [message for message in messages if message["id"] not in raw_ids]
            row["liveMessages"] = len(live_ids)
            row["missingBeforeRepair"] = len(missing)
            row["unverifiableEmptyLive"] = bool(not messages and row["stateCursor"] and not raw_ids)
            row["appended"] = 0
            if args.apply and missing:
                worker.append_batch(root, entry, missing, f"full-history reconcile {datetime.now().isoformat()}")
                row["appended"] = len(missing)
            if args.apply and messages:
                now = datetime.now(timezone.utc).isoformat()
                latest = max(live_ids, key=int)
                entry.update({
                    "lastWrittenMessageId": latest,
                    "lastMessageId": latest,
                    "lastBackup": args.today,
                    "lastSuccessfulWriteAt": now if missing else entry.get("lastSuccessfulWriteAt"),
                    "syncStatus": "healthy",
                    "backlogReason": None,
                    "consecutiveErrors": 0,
                    "updatedAt": now,
                })
                mark_queue_caught_up(queue, key, latest, now)
                state["updatedAt"] = now
                queue["updatedAt"] = now
                atomic_json(state_path, state)
                atomic_json(queue_path, queue)
        except Exception as exc:
            row["liveError"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)

    result = {
        "ok": not any(row.get("liveError") for row in rows),
        "mode": "local" if args.local_only else ("apply" if args.apply else "live-dry-run"),
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "summary": summarize(rows),
        "entries": rows,
        "activeQueue": sum(1 for item in queue.get("items") or [] if item.get("status") in ACTIVE_QUEUE),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.compact:
        summary = result["summary"]
        print(
            "[raw-integrity] "
            f"mode={result['mode']} entries={summary['entries']} "
            f"liveMessages={summary['liveMessages']} "
            f"missingBeforeRepair={summary['missingBeforeRepair']} "
            f"appended={summary['appended']} liveErrors={summary['liveErrors']} "
            f"activeQueue={result['activeQueue']}"
        )
    else:
        print(json.dumps({k: v for k, v in result.items() if k != "entries"}, ensure_ascii=False, indent=2))
    if lock_handle is not None:
        lock_handle.close()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
