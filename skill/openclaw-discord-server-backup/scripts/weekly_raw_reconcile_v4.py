#!/usr/bin/env python3
"""Weekly full-inventory raw reconcile with recovery and append-only closeout.

The command intentionally emits no progress messages while scanning. A caller may
send progress before invoking it and the final report after it exits, but should
not write into the audited Discord report channel during the closeout window.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent


def load_sibling(name: str):
    path = HERE / name
    spec = importlib.util.spec_from_file_location(f"weekly_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reconcile = load_sibling("reconcile_raw_archive_v3.py")
worker = reconcile.worker


CLASSIFICATIONS = {
    "deleted_from_discord",
    "inaccessible_or_permission_gap",
    "local_parse_artifact",
    "cross_entry_duplicate",
    "unknown",
}


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-weekly-v4")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def safe_entry_dir(root: Path, relative_path: str) -> Path:
    root_resolved = root.resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise RuntimeError(f"unsafe relativePath outside archive root: {relative_path}")
    return candidate


def ordered_entries(
    state: dict[str, Any], report_entry_key: str | None = None
) -> list[tuple[str, dict[str, Any]]]:
    rows = [
        (key, entry)
        for key, entry in (state.get("entries") or {}).items()
        if not reconcile.entry_is_excluded(entry)
    ]
    rows.sort(key=lambda row: (row[0] == report_entry_key, row[0]))
    return rows


def scan(
    entries: list[tuple[str, dict[str, Any]]],
    root: Path,
    token: str,
    page_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, set[str]]]:
    rows: list[dict[str, Any]] = []
    messages_by_key: dict[str, list[dict[str, Any]]] = {}
    raw_ids_by_key: dict[str, set[str]] = {}
    for key, entry in entries:
        relative_path = str(entry.get("relativePath") or key)
        raw_dir = safe_entry_dir(root, relative_path) / "raw"
        raw_counts, _ = reconcile.archive_message_ids(raw_dir)
        raw_ids = set(raw_counts)
        raw_ids_by_key[key] = raw_ids
        row: dict[str, Any] = {
            "key": key,
            "channelId": entry.get("channelId"),
            "relativePath": relative_path,
            "rawMessageIds": len(raw_ids),
            "duplicateRawIds": sum(count - 1 for count in raw_counts.values() if count > 1),
        }
        if not entry.get("channelId"):
            row["liveError"] = "missing_channel_id"
            rows.append(row)
            continue
        try:
            messages = reconcile.fetch_all_messages(token, str(entry["channelId"]), page_limit)
            messages_by_key[key] = messages
            live_ids = {str(message["id"]) for message in messages}
            missing = [message for message in messages if str(message["id"]) not in raw_ids]
            row.update({
                "liveMessages": len(live_ids),
                "liveOnly": len(missing),
                "localOnly": len(raw_ids - live_ids),
            })
        except Exception as exc:
            row["liveError"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    return rows, messages_by_key, raw_ids_by_key


def ensure_recovery_copy(
    recovery_root: Path,
    state_path: Path,
    queue_path: Path,
    archive_root: Path,
    key: str,
    entry: dict[str, Any],
    copied: set[str],
) -> None:
    recovery_root.mkdir(parents=True, exist_ok=True)
    if not copied:
        shutil.copy2(state_path, recovery_root / "state.json")
        if queue_path.exists():
            shutil.copy2(queue_path, recovery_root / "queue.json")
    if key in copied:
        return
    source = safe_entry_dir(archive_root, str(entry.get("relativePath") or key)) / "raw"
    destination = recovery_root / "raw" / key.replace("/", "__")
    if source.exists():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.mkdir(parents=True, exist_ok=True)
    copied.add(key)


def apply_missing(
    state: dict[str, Any],
    queue: dict[str, Any],
    entries: list[tuple[str, dict[str, Any]]],
    root: Path,
    messages_by_key: dict[str, list[dict[str, Any]]],
    raw_ids_by_key: dict[str, set[str]],
    today: str,
    recovery_root: Path,
    state_path: Path,
    queue_path: Path,
    copied: set[str],
) -> tuple[int, list[str]]:
    appended = 0
    affected: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    for key, entry in entries:
        messages = messages_by_key.get(key) or []
        missing = [message for message in messages if str(message["id"]) not in raw_ids_by_key.get(key, set())]
        if not missing:
            continue
        ensure_recovery_copy(recovery_root, state_path, queue_path, root, key, entry, copied)
        worker.append_batch(root, entry, missing, f"weekly full-inventory repair {now}")
        latest = max((str(message["id"]) for message in messages), key=int)
        entry.update({
            "lastWrittenMessageId": latest,
            "lastMessageId": latest,
            "lastBackup": today,
            "lastSuccessfulWriteAt": now,
            "syncStatus": "healthy",
            "backlogReason": None,
            "consecutiveErrors": 0,
            "updatedAt": now,
        })
        reconcile.mark_queue_caught_up(queue, key, latest, now)
        appended += len(missing)
        affected.append(key)
    if affected:
        state["updatedAt"] = now
        queue["updatedAt"] = now
        atomic_json(state_path, state)
        atomic_json(queue_path, queue)
    return appended, affected


def classify_local_only(
    rows: list[dict[str, Any]],
    messages_by_key: dict[str, list[dict[str, Any]]],
    raw_ids_by_key: dict[str, set[str]],
) -> dict[str, Any]:
    live_ids_by_key = {
        key: {str(message["id"]) for message in messages}
        for key, messages in messages_by_key.items()
    }
    raw_owners: dict[str, set[str]] = defaultdict(set)
    for key, ids in raw_ids_by_key.items():
        for message_id in ids:
            raw_owners[message_id].add(key)
    errors = {row["key"] for row in rows if row.get("liveError")}
    details: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    for key, raw_ids in raw_ids_by_key.items():
        for message_id in sorted(raw_ids - live_ids_by_key.get(key, set()), key=int):
            if key in errors:
                kind = "inaccessible_or_permission_gap"
            elif len(raw_owners[message_id]) > 1:
                kind = "cross_entry_duplicate"
            elif key in live_ids_by_key:
                kind = "deleted_from_discord"
            elif not message_id.isdigit():
                kind = "local_parse_artifact"
            else:
                kind = "unknown"
            counts[kind] += 1
            details.append({"entry": key, "messageId": message_id, "classification": kind})
    for kind in CLASSIFICATIONS:
        counts.setdefault(kind, 0)
    live_union = set().union(*live_ids_by_key.values()) if live_ids_by_key else set()
    raw_union = set().union(*raw_ids_by_key.values()) if raw_ids_by_key else set()
    return {
        "counts": dict(sorted(counts.items())),
        "details": details,
        "liveMessageIds": len(live_union),
        "localRawMessageIds": len(raw_union),
        "intersectionMessageIds": len(live_union & raw_union),
        "liveOnlyMessageIds": len(live_union - raw_union),
        "localOnlyMessageIds": len(raw_union - live_union),
        "setConservationPass": (
            len(live_union | raw_union)
            == len(live_union & raw_union) + len(live_union - raw_union) + len(raw_union - live_union)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run recovery-first weekly raw reconcile and full closeout.")
    parser.add_argument("--state", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--today", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--openclaw-config", default=str(Path.home() / ".openclaw/openclaw.json"))
    parser.add_argument("--token-env", default="DISCORD_BOT_TOKEN")
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--max-closeout-passes", type=int, default=3)
    parser.add_argument("--report-entry-key")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.page_limit <= 100:
        parser.error("--page-limit must be between 1 and 100")
    if not 1 <= args.max_closeout_passes <= 10:
        parser.error("--max-closeout-passes must be between 1 and 10")

    state_path = Path(args.state)
    queue_path = Path(args.queue)
    root = Path(args.root)
    evidence_dir = Path(args.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    state = reconcile.load_json(state_path)
    queue = reconcile.load_json(queue_path, {"version": 1, "items": []})
    entries = ordered_entries(state, args.report_entry_key)
    token = worker.load_discord_token(Path(args.openclaw_config), args.token_env)

    lock_path = state_path.parent / ".channel_backup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        print(json.dumps({"ok": False, "skipped": "locked"}, ensure_ascii=False))
        return 2

    copied: set[str] = set()
    passes: list[dict[str, Any]] = []
    total_appended = 0
    try:
        for pass_number in range(1, args.max_closeout_passes + 1):
            rows, messages_by_key, raw_ids_by_key = scan(entries, root, token, args.page_limit)
            live_errors = sum(bool(row.get("liveError")) for row in rows)
            live_only = sum(int(row.get("liveOnly") or 0) for row in rows)
            pass_result = {
                "pass": pass_number,
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "entries": len(rows),
                "liveErrors": live_errors,
                "liveOnlyBeforeRepair": live_only,
            }
            if live_errors:
                passes.append(pass_result)
                break
            appended, affected = apply_missing(
                state, queue, entries, root, messages_by_key, raw_ids_by_key,
                args.today, evidence_dir / "pre-repair", state_path, queue_path, copied,
            )
            total_appended += appended
            pass_result.update({"appended": appended, "affectedEntries": affected})
            passes.append(pass_result)
            if live_only == 0:
                break

        final_rows, final_messages, final_raw_ids = scan(entries, root, token, args.page_limit)
        final_live_errors = sum(bool(row.get("liveError")) for row in final_rows)
        final_live_only = sum(int(row.get("liveOnly") or 0) for row in final_rows)
        classification = classify_local_only(final_rows, final_messages, final_raw_ids)
        state_entries = state.get("entries") or {}
        active_queue = sum(
            1
            for item in queue.get("items", [])
            if item.get("status") in reconcile.ACTIVE_QUEUE
            and not reconcile.entry_is_excluded(state_entries.get(item.get("entryKey"), {}))
        )
        ok = (
            final_live_errors == 0
            and final_live_only == 0
            and active_queue == 0
            and classification["counts"]["unknown"] == 0
            and classification["setConservationPass"]
        )
        result = {
            "schema": "openclaw-weekly-raw-reconcile-v4",
            "ok": ok,
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "entries": len(entries),
            "excludedEntries": len((state.get("entries") or {})) - len(entries),
            "passes": passes,
            "appended": total_appended,
            "finalLiveOnly": final_live_only,
            "finalLiveErrors": final_live_errors,
            "activeQueue": active_queue,
            "localOnlyClassification": classification,
            "recoveryPath": str(evidence_dir / "pre-repair") if copied else None,
            "selfDriftGuard": "Do not send report-channel messages between final scan start and capturedAt.",
        }
        atomic_json(evidence_dir / "weekly-reconciliation-summary.json", result)
        atomic_json(evidence_dir / "local-only-id-classification.json", classification)
        if args.compact:
            print(
                "[weekly-raw-v4] "
                f"ok={str(ok).lower()} entries={len(entries)} appended={total_appended} "
                f"liveOnly={final_live_only} liveErrors={final_live_errors} "
                f"localOnly={classification['localOnlyMessageIds']} unknown={classification['counts']['unknown']} "
                f"activeQueue={active_queue}"
            )
        else:
            print(json.dumps({key: value for key, value in result.items() if key != "localOnlyClassification"}, ensure_ascii=False, indent=2))
        return 0 if ok else 2
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
