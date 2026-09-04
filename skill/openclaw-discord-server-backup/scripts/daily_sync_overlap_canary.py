#!/usr/bin/env python3
"""Small process used by cron-manager to prove two shared-session runs do not overlap."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def append_event(path: Path, event: dict[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description="Persistent-session overlap canary worker.")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--hold-ms", type=int, default=750)
    args = parser.parse_args()
    root = Path(args.state_dir).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    active = root / "active"
    overlap = root / "overlap"
    trace = root / "trace.jsonl"
    try:
        descriptor = os.open(active, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        overlap.write_text(args.label + "\n", encoding="utf-8")
        os.chmod(overlap, 0o600)
        append_event(trace, {"event": "overlap", "label": args.label, "at": datetime.now(timezone.utc).isoformat()})
        return 3
    os.close(descriptor)
    try:
        append_event(trace, {"event": "start", "label": args.label, "at": datetime.now(timezone.utc).isoformat()})
        time.sleep(max(1, args.hold_ms) / 1000)
        append_event(trace, {"event": "end", "label": args.label, "at": datetime.now(timezone.utc).isoformat()})
    finally:
        active.unlink(missing_ok=True)
    print("SERIAL_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
