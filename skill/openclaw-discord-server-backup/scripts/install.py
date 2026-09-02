#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from backup_paths import build_layout, resolved, validate_layout

DEFAULT_CONFIG = {
    "guildId": "CHANGE_ME",
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


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"existing JSON cannot be read safely: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"existing JSON must contain an object: {path}")
    return data


def workspace_path(workspace: Path, value: str, label: str) -> Path:
    candidate = resolved(workspace / value)
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError(f"{label} must stay inside the OpenClaw workspace")
    return candidate


def main() -> int:
    ap = argparse.ArgumentParser(description="Install OpenClaw Discord server backup skill scaffold.")
    ap.add_argument("--workspace", default="~/.openclaw/workspace")
    ap.add_argument("--skill-dir", default=None)
    ap.add_argument("--config", default="memory/openclaw_discord_backup_config.json")
    ap.add_argument("--server-name", help="Discord server display name; creates <name>資料備份 on Desktop")
    ap.add_argument("--backup-root", help="Explicit custom backup root; disables automatic Desktop naming")
    ap.add_argument("--desktop-dir", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    workspace = resolved(Path(args.workspace))
    if not workspace.exists() or not workspace.is_dir():
        raise SystemExit(f"OpenClaw workspace does not exist or is not a directory: {workspace}")
    source_skill = Path(__file__).resolve().parents[1]
    target_skill = resolved(Path(args.skill_dir)) if args.skill_dir else workspace / "skills" / source_skill.name
    config_path = workspace_path(workspace, args.config, "config path")

    try:
        layout = build_layout(
            server_name=args.server_name,
            backup_root=args.backup_root,
            desktop_dir=args.desktop_dir,
        )
        validate_layout(layout, workspace)
    except ValueError as exc:
        raise SystemExit(f"Unsafe backup layout: {exc}") from exc

    state_relative = DEFAULT_CONFIG["statePath"]
    queue_relative = DEFAULT_CONFIG["queuePath"]
    existing_config = read_json(config_path) if config_path.exists() else None
    if existing_config is not None:
        configured_root = existing_config.get("backupRoot")
        if not isinstance(configured_root, str) or not configured_root.strip():
            raise SystemExit(f"Existing config has no valid backupRoot: {config_path}")
        if resolved(Path(configured_root)) != layout.discord_root:
            raise SystemExit(
                "Existing backup root differs from the requested Desktop layout; "
                "migration is required and no customer data was changed."
            )
        state_relative = str(existing_config.get("statePath", state_relative))
        queue_relative = str(existing_config.get("queuePath", queue_relative))

    state_path = workspace_path(workspace, state_relative, "state path")
    queue_path = workspace_path(workspace, queue_relative, "queue path")
    existing_state = read_json(state_path) if state_path.exists() else None
    if existing_state is not None:
        state_root = existing_state.get("rootPath")
        if not isinstance(state_root, str) or resolved(Path(state_root)) != layout.discord_root:
            raise SystemExit(
                "Existing state root differs from the requested Desktop layout; "
                "migration is required and no customer data was changed."
            )

    if target_skill.exists() and not args.force:
        raise SystemExit(f"Skill already exists: {target_skill}. Use --force to overwrite.")
    if target_skill.exists():
        shutil.rmtree(target_skill)
    shutil.copytree(source_skill, target_skill)

    layout.backup_root.mkdir(parents=False, exist_ok=True)
    layout.discord_root.mkdir(parents=False, exist_ok=True)

    if existing_config is None:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["backupRoot"] = str(layout.discord_root)
        write_json(config_path, cfg)

    if existing_state is None:
        write_json(state_path, {
            "version": 3,
            "schema": "channel-backup-state-v3",
            "guildId": "CHANGE_ME",
            "rootPath": str(layout.discord_root),
            "queuePath": queue_relative,
            "entries": {}
        })
    if not queue_path.exists():
        write_json(queue_path, {"version": 1, "items": []})

    print(json.dumps({
        "installedSkill": str(target_skill),
        "backupRoot": str(layout.backup_root),
        "discordDataRoot": str(layout.discord_root),
        "coreDataRoot": str(layout.core_root),
        "config": str(config_path),
        "state": str(state_path),
        "queue": str(queue_path),
        "next": "Edit config, then create Discord and core-backup cron jobs from examples/cron.examples.md"
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
