#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_payload(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    for key in ("items", "channels", "threads", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    raise ValueError(f"Unsupported inventory payload shape in {path}")


def pick(obj: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return None


def ensure_entry_dirs(root: Path, relative_path: str, entry_type: str) -> None:
    base = root / relative_path
    for name in ("raw", "summary", "legacy"):
        (base / name).mkdir(parents=True, exist_ok=True)
    if entry_type == "thread":
        (base / "legacy_docs").mkdir(parents=True, exist_ok=True)


def merge_entry(existing: dict[str, Any] | None, new_entry: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    merged.update({k: v for k, v in new_entry.items() if v is not None})
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap or refresh channel/thread backup state.")
    parser.add_argument("--state", required=True, help="Path to state JSON file")
    parser.add_argument("--root", required=True, help="Backup root directory")
    parser.add_argument("--channels", help="JSON file with channel inventory")
    parser.add_argument("--threads", help="JSON file with thread inventory")
    parser.add_argument("--guild-id", help="Optional guild/server id")
    args = parser.parse_args()

    state_path = Path(args.state)
    root = Path(args.root)

    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {
            "version": 2,
            "guildId": args.guild_id,
            "rootPath": str(root),
            "structure": {
                "channel": "<頻道名>/{raw,summary,legacy}/",
                "thread": "<頻道名>/<討論串名>/{raw,summary,legacy,legacy_docs}/",
            },
            "entries": {},
        }

    state["version"] = 2
    state["rootPath"] = str(root)
    if args.guild_id:
        state["guildId"] = args.guild_id
    state.setdefault("structure", {
        "channel": "<頻道名>/{raw,summary,legacy}/",
        "thread": "<頻道名>/<討論串名>/{raw,summary,legacy,legacy_docs}/",
    })
    state.setdefault("entries", {})

    summary = {"channels": 0, "threads": 0, "created": 0, "updated": 0}

    if args.channels:
        for item in load_payload(Path(args.channels)):
            name = pick(item, "name", "channelName")
            channel_id = str(pick(item, "id", "channelId") or "")
            if not name or not channel_id:
                continue
            key = str(name)
            entry = {
                "type": "channel",
                "channelId": channel_id,
                "relativePath": key,
                "guildId": args.guild_id or state.get("guildId"),
            }
            existed = key in state["entries"]
            state["entries"][key] = merge_entry(state["entries"].get(key), entry)
            ensure_entry_dirs(root, key, "channel")
            summary["channels"] += 1
            summary["updated" if existed else "created"] += 1

    if args.threads:
        for item in load_payload(Path(args.threads)):
            name = pick(item, "name", "threadName")
            thread_id = str(pick(item, "id", "threadId", "channelId") or "")
            parent = pick(item, "parentChannel", "parentName", "parent")
            if not name or not thread_id or not parent:
                continue
            key = f"{parent}/{name}"
            entry = {
                "type": "thread",
                "channelId": thread_id,
                "parentChannel": str(parent),
                "relativePath": key,
                "guildId": args.guild_id or state.get("guildId"),
            }
            existed = key in state["entries"]
            state["entries"][key] = merge_entry(state["entries"].get(key), entry)
            ensure_entry_dirs(root, key, "thread")
            summary["threads"] += 1
            summary["updated" if existed else "created"] += 1

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
