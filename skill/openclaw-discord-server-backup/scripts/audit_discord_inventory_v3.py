#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_BASE = "https://discord.com/api/v10"
TEXT_CHANNEL_TYPES = {0, 5}
THREAD_PARENT_TYPES = {0, 5, 15, 16}
MAX_429_RETRIES = 8


def load_discord_token(config_path: Path | None, env_name: str) -> str:
    token = os.getenv(env_name)
    if token:
        return token
    if config_path and config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        token = (((config.get("channels") or {}).get("discord") or {}).get("token"))
        if token:
            return token
    raise RuntimeError(f"Discord token not found. Set {env_name} or pass --openclaw-config.")


class DiscordClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(
            f"{API_BASE}{path}{query}",
            headers={
                "Authorization": f"Bot {self.token}",
                "User-Agent": "openclaw-discord-inventory-v3/1.0",
            },
        )
        retries = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="ignore")
                if exc.code == 429:
                    retries += 1
                    if retries > MAX_429_RETRIES:
                        raise RuntimeError(
                            f"Discord 429 persisted after {MAX_429_RETRIES} retries: {body[:200]}"
                        ) from exc
                    try:
                        retry_after = float(json.loads(body).get("retry_after", 1.0))
                    except Exception:
                        retry_after = 1.0
                    time.sleep(min(retry_after + 0.25, 30))
                    continue
                raise RuntimeError(f"Discord HTTP {exc.code} for {path}: {body[:300]}") from exc


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item.get("id") or "")
        if item_id:
            by_id[item_id] = item
    return sorted(by_id.values(), key=lambda item: (str(item.get("parent_id") or ""), str(item.get("name") or ""), str(item.get("id") or "")))


def archived_threads(
    client: DiscordClient,
    channel_id: str,
    endpoint: str,
    *,
    page_limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    before: str | None = None
    for _ in range(page_limit):
        params = {"limit": "100"}
        if before:
            params["before"] = before
        payload = client.get(endpoint.format(channel_id=channel_id), params)
        threads = payload.get("threads") or []
        rows.extend(threads)
        if not payload.get("has_more") or not threads:
            break
        timestamps = [
            ((thread.get("thread_metadata") or {}).get("archive_timestamp"))
            for thread in threads
        ]
        timestamps = [value for value in timestamps if value]
        if not timestamps:
            break
        next_before = min(timestamps)
        if next_before == before:
            break
        before = next_before
    return rows


def collect_inventory(
    client: DiscordClient,
    guild_id: str,
    *,
    archived_page_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    all_channels = client.get(f"/guilds/{guild_id}/channels")
    channels = [item for item in all_channels if int(item.get("type", -1)) in TEXT_CHANNEL_TYPES]
    parents = [item for item in all_channels if int(item.get("type", -1)) in THREAD_PARENT_TYPES]
    parent_names = {str(item.get("id")): str(item.get("name") or item.get("id")) for item in parents}
    warnings: list[dict[str, str]] = []

    active_payload = client.get(f"/guilds/{guild_id}/threads/active")
    threads = list(active_payload.get("threads") or [])
    endpoints = (
        ("public", "/channels/{channel_id}/threads/archived/public"),
        ("private", "/channels/{channel_id}/threads/archived/private"),
        ("joined_private", "/channels/{channel_id}/users/@me/threads/archived/private"),
    )
    for parent in parents:
        channel_id = str(parent.get("id"))
        for kind, endpoint in endpoints:
            try:
                threads.extend(
                    archived_threads(
                        client,
                        channel_id,
                        endpoint,
                        page_limit=archived_page_limit,
                    )
                )
            except RuntimeError as exc:
                warnings.append({"channelId": channel_id, "kind": kind, "error": str(exc)})

    normalized_threads = []
    for thread in dedupe(threads):
        row = dict(thread)
        parent_id = str(row.get("parent_id") or "")
        row["parentName"] = parent_names.get(parent_id, parent_id or "unknown-parent")
        normalized_threads.append(row)
    return dedupe(channels), normalized_threads, warnings


def compare_state(
    state: dict[str, Any],
    channels: list[dict[str, Any]],
    threads: list[dict[str, Any]],
) -> dict[str, Any]:
    entries = state.get("entries") or {}
    state_by_id = {
        str(entry.get("channelId")): {"key": key, **entry}
        for key, entry in entries.items()
        if entry.get("channelId")
    }
    live_rows: list[dict[str, Any]] = []
    for channel in channels:
        live_rows.append({
            "type": "channel",
            "id": str(channel.get("id")),
            "name": channel.get("name"),
            "relativePath": channel.get("name"),
        })
    for thread in threads:
        parent = str(thread.get("parentName") or thread.get("parent_id") or "unknown-parent")
        name = str(thread.get("name") or thread.get("id"))
        live_rows.append({
            "type": "thread",
            "id": str(thread.get("id")),
            "name": name,
            "parentId": str(thread.get("parent_id") or ""),
            "parentName": parent,
            "relativePath": f"{parent}/{name}",
        })
    live_by_id = {row["id"]: row for row in live_rows if row["id"]}
    missing = [live_by_id[item_id] for item_id in sorted(set(live_by_id) - set(state_by_id))]
    orphaned = [state_by_id[item_id] for item_id in sorted(set(state_by_id) - set(live_by_id))]
    mismatched = []
    for item_id in sorted(set(live_by_id) & set(state_by_id)):
        live = live_by_id[item_id]
        entry = state_by_id[item_id]
        if str(entry.get("type")) != live["type"]:
            mismatched.append({"id": item_id, "state": entry, "live": live, "reason": "type"})
    return {
        "liveChannels": len(channels),
        "liveThreads": len(threads),
        "liveEntries": len(live_by_id),
        "stateEntries": len(entries),
        "missingFromState": missing,
        "orphanedStateEntries": orphaned,
        "typeMismatches": mismatched,
    }


def safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[\\/]", "／", value).strip().strip(".")
    return cleaned or fallback


def unique_key(entries: dict[str, Any], preferred: str, item_id: str) -> str:
    existing = entries.get(preferred)
    if not existing or str(existing.get("channelId") or "") == item_id:
        return preferred
    return f"{preferred} ({item_id})"


def register_missing(
    state_path: Path,
    root: Path,
    guild_id: str,
    missing: list[dict[str, Any]],
) -> list[dict[str, str]]:
    lock_path = state_path.parent / ".channel_backup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock_handle.close()
        raise RuntimeError("backup state is locked by another job") from exc
    registered: list[dict[str, str]] = []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        entries = state.setdefault("entries", {})
        known_ids = {str(entry.get("channelId")) for entry in entries.values() if entry.get("channelId")}
        for item in missing:
            item_id = str(item.get("id") or "")
            if not item_id or item_id in known_ids:
                continue
            name = safe_name(str(item.get("name") or ""), item_id)
            if item.get("type") == "thread":
                parent = safe_name(str(item.get("parentName") or ""), str(item.get("parentId") or "unknown-parent"))
                preferred = f"{parent}/{name}"
                entry_type = "thread"
            else:
                parent = ""
                preferred = name
                entry_type = "channel"
            key = unique_key(entries, preferred, item_id)
            entry = {
                "type": entry_type,
                "channelId": item_id,
                "relativePath": key,
                "guildId": guild_id,
                "lastMessageId": None,
                "lastWrittenMessageId": None,
                "lastBackup": None,
                "syncStatus": "healthy",
                "backlogReason": None,
                "consecutiveErrors": 0,
            }
            if parent:
                entry["parentChannel"] = parent
            entries[key] = entry
            base = root / key
            for dirname in ("raw", "summary", "legacy"):
                (base / dirname).mkdir(parents=True, exist_ok=True)
            if entry_type == "thread":
                (base / "legacy_docs").mkdir(parents=True, exist_ok=True)
            registered.append({"key": key, "channelId": item_id, "type": entry_type})
            known_ids.add(item_id)
        if registered:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(state_path, state_path.with_name(state_path.name + f".bak-inventory-{stamp}"))
            state["updatedAt"] = datetime.now(timezone.utc).isoformat()
            temp = state_path.with_name(state_path.name + ".tmp")
            temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, state_path)
        return registered
    finally:
        lock_handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare the full visible Discord guild inventory with backup state.")
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--root")
    parser.add_argument("--openclaw-config", default=str(Path.home() / ".openclaw/openclaw.json"))
    parser.add_argument("--token-env", default="DISCORD_BOT_TOKEN")
    parser.add_argument("--archived-page-limit", type=int, default=100)
    parser.add_argument("--out")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    token = load_discord_token(Path(args.openclaw_config), args.token_env)
    channels, threads, warnings = collect_inventory(
        DiscordClient(token),
        args.guild_id,
        archived_page_limit=args.archived_page_limit,
    )
    coverage = compare_state(state, channels, threads)
    registered: list[dict[str, str]] = []
    if args.apply:
        if not args.root:
            parser.error("--root is required with --apply")
        registered = register_missing(
            Path(args.state),
            Path(args.root),
            args.guild_id,
            coverage["missingFromState"],
        )
    remaining_missing = max(0, len(coverage["missingFromState"]) - len(registered))
    result = {
        "ok": remaining_missing == 0 and not coverage["typeMismatches"],
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "guildId": args.guild_id,
        "coverage": coverage,
        "registered": registered,
        "remainingMissing": remaining_missing,
        "warnings": warnings,
        "channels": channels,
        "threads": threads,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.compact:
        print(
            "[inventory-audit] "
            f"liveChannels={coverage['liveChannels']} liveThreads={coverage['liveThreads']} "
            f"stateEntries={coverage['stateEntries']} missingFromState={len(coverage['missingFromState'])} "
            f"registered={len(registered)} remainingMissing={remaining_missing} "
            f"orphanedState={len(coverage['orphanedStateEntries'])} "
            f"typeMismatches={len(coverage['typeMismatches'])} warnings={len(warnings)}"
        )
    else:
        print(json.dumps({key: value for key, value in result.items() if key not in {"channels", "threads"}}, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
