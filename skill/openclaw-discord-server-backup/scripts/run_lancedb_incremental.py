#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.expanduser().read_text(encoding="utf-8"))


def resolve_path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def main() -> int:
    ap = argparse.ArgumentParser(description="Run optional LanceDB incremental indexing after Discord backup.")
    ap.add_argument("--config", required=True, help="Customer backup config JSON")
    ap.add_argument("--workspace", default="~/.openclaw/workspace")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    cfg = load_json(Path(args.config))
    lancedb = cfg.get("lancedb") or {}
    if not lancedb.get("enabled"):
        print(json.dumps({"ok": True, "skipped": True, "reason": "lancedb.disabled"}, ensure_ascii=False, indent=2))
        return 0

    project = resolve_path(workspace, lancedb.get("projectPath", "knowledge-lancedb"))
    command = lancedb.get("incrementalCommand") or "npm run incremental"
    report = resolve_path(workspace, lancedb.get("latestManifest", "knowledge-lancedb/reports/incremental-manifest.latest.json"))

    if args.dry_run:
        print(json.dumps({"ok": True, "dryRun": True, "projectPath": str(project), "command": command, "latestManifest": str(report)}, ensure_ascii=False, indent=2))
        return 0

    proc = subprocess.run(command, cwd=project, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    manifest = None
    if report.exists():
        try:
            manifest = json.loads(report.read_text(encoding="utf-8"))
        except Exception as exc:
            manifest = {"readError": repr(exc)}
    result = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "projectPath": str(project),
        "command": command,
        "outputTail": proc.stdout[-4000:],
        "manifest": manifest,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
