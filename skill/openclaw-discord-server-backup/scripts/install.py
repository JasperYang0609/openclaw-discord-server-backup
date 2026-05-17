#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

DEFAULT_CONFIG = {
    "guildId": "CHANGE_ME",
    "backupRoot": "~/OpenClawBackups/discord",
    "statePath": "memory/channel_backup_summary_state.json",
    "queuePath": "memory/channel_backup_backlog_queue.json",
    "reportChannel": "discord:channel:CHANGE_ME",
    "timezone": "Asia/Taipei",
    "limits": {
        "dailyEntryLimit": 6,
        "dailyMessageLimitPerEntry": 60,
        "backlogEntryLimit": 4,
        "backlogBatchLimit": 12,
        "backlogPageLimit": 100,
        "auditProbeLimit": 1
    }
}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Install OpenClaw Discord server backup skill scaffold.")
    ap.add_argument("--workspace", default="~/.openclaw/workspace")
    ap.add_argument("--skill-dir", default=None)
    ap.add_argument("--config", default="memory/openclaw_discord_backup_config.json")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    source_skill = Path(__file__).resolve().parents[1]
    target_skill = Path(args.skill_dir).expanduser().resolve() if args.skill_dir else workspace / "skills" / source_skill.name
    config_path = workspace / args.config

    if target_skill.exists() and not args.force:
        raise SystemExit(f"Skill already exists: {target_skill}. Use --force to overwrite.")
    if target_skill.exists():
        shutil.rmtree(target_skill)
    shutil.copytree(source_skill, target_skill)

    if not config_path.exists():
        cfg = dict(DEFAULT_CONFIG)
        cfg["backupRoot"] = str((Path.home() / "OpenClawBackups" / "discord").expanduser())
        write_json(config_path, cfg)

    state_path = workspace / DEFAULT_CONFIG["statePath"]
    queue_path = workspace / DEFAULT_CONFIG["queuePath"]
    if not state_path.exists():
        write_json(state_path, {
            "version": 3,
            "schema": "channel-backup-state-v3",
            "guildId": "CHANGE_ME",
            "rootPath": DEFAULT_CONFIG["backupRoot"],
            "queuePath": DEFAULT_CONFIG["queuePath"],
            "entries": {}
        })
    if not queue_path.exists():
        write_json(queue_path, {"version": 1, "items": []})

    print(json.dumps({
        "installedSkill": str(target_skill),
        "config": str(config_path),
        "state": str(state_path),
        "queue": str(queue_path),
        "next": "Edit config, then create OpenClaw cron jobs from examples/cron.examples.md"
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
