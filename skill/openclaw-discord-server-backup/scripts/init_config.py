#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backup_paths import build_layout


def main() -> int:
    ap = argparse.ArgumentParser(description="Create customer config for OpenClaw Discord backup skill.")
    ap.add_argument("--out", required=True)
    ap.add_argument("--guild-id", default="CHANGE_ME")
    ap.add_argument("--server-name", help="Discord server display name used for <name>資料備份")
    ap.add_argument("--backup-root", help="Explicit custom backup root")
    ap.add_argument("--desktop-dir", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--report-channel", default="discord:channel:CHANGE_ME")
    ap.add_argument("--timezone", default="Asia/Taipei")
    args = ap.parse_args()
    try:
        layout = build_layout(
            server_name=args.server_name,
            backup_root=args.backup_root,
            desktop_dir=args.desktop_dir,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    cfg = {
        "guildId": args.guild_id,
        "backupRoot": str(layout.discord_root),
        "statePath": "memory/channel_backup_summary_state.json",
        "queuePath": "memory/channel_backup_backlog_queue.json",
        "reportChannel": args.report_channel,
        "timezone": args.timezone,
        "limits": {
            "dailyEntryLimit": 6,
            "dailyMessageLimitPerEntry": 60,
            "dailyLookbackLimit": 10,
            "dailyFreshnessDays": 2,
            "backlogEntryLimit": 4,
            "backlogBatchLimit": 12,
            "backlogPageLimit": 100,
            "auditProbeLimit": 1
        }
    }
    p = Path(args.out).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
