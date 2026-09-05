#!/usr/bin/env python3
"""Prove that deterministic daily slots share one non-blocking file lock."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import time
from pathlib import Path


def append_event(path: Path, event: dict[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily sync shared-lock canary worker.")
    parser.add_argument("--lock", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--label", required=True, choices=("A", "B"))
    parser.add_argument("--hold-ms", type=int, default=500)
    args = parser.parse_args()
    lock = Path(args.lock)
    trace = Path(args.trace)
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock, flags, 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
        os.close(descriptor)
        raise SystemExit("unsafe lock target")
    os.fchmod(descriptor, 0o600)
    handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            append_event(trace, {"event": "skipped", "label": args.label})
            print(json.dumps({"ok": False, "status": "skipped", "reason": "backup_lock_busy"}, sort_keys=True))
            return 0
        append_event(trace, {"event": "start", "label": args.label})
        time.sleep(max(1, min(args.hold_ms, 5000)) / 1000)
        append_event(trace, {"event": "end", "label": args.label})
        print(json.dumps({"ok": True, "status": "completed"}, sort_keys=True))
        return 0
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
