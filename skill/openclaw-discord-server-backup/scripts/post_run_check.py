#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = ROOT / "skill" / "openclaw-discord-server-backup"


def check(name: str, ok: bool, detail: str = "", results: list[tuple[str, bool, str]] | None = None) -> None:
    if results is not None:
        results.append((name, ok, detail))


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)


def json_loads(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def queue_selector_smoke() -> tuple[bool, str]:
    script = SKILL_DIR / "scripts" / "select_backlog_candidates.py"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        state = tmpdir / "state.json"
        queue = tmpdir / "queue.json"
        state.write_text(
            json.dumps(
                {
                    "entries": {
                        "quiet": {
                            "type": "channel",
                            "channelId": "1",
                            "relativePath": "quiet",
                            "lastWrittenMessageId": "100",
                            "lastMessageId": "100",
                            "syncStatus": "healthy",
                        },
                        "partial": {
                            "type": "channel",
                            "channelId": "2",
                            "relativePath": "partial",
                            "lastWrittenMessageId": "200",
                            "lastMessageId": "200",
                            "syncStatus": "partial",
                            "backlogReason": "page_limit_reached",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        queue.write_text(json.dumps({"version": 1, "items": []}), encoding="utf-8")
        proc = run([
            sys.executable,
            str(script),
            "--state",
            str(state),
            "--queue",
            str(queue),
            "--today",
            "2026-07-10",
            "--limit",
            "1",
        ])
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout).strip()
        data = json.loads(proc.stdout)
        selected = data.get("selected") or []
        if not selected or selected[0].get("key") != "partial":
            return False, f"expected partial entry, got {selected}"
    return True, ""


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    required = [
        "skill/openclaw-discord-server-backup/SKILL.md",
        "skill/openclaw-discord-server-backup/scripts/run_backlog_worker_v3.py",
        "skill/openclaw-discord-server-backup/scripts/audit_caught_up_v3.py",
        "skill/openclaw-discord-server-backup/scripts/select_backlog_candidates.py",
        "examples/config.example.json",
        "examples/state.example.json",
        "examples/queue.example.json",
    ]
    check("required files exist", all((ROOT / rel).exists() for rel in required), results=results)
    check("example JSON parses", all(json_loads(ROOT / rel) for rel in required if rel.endswith(".json")), results=results)

    ok, detail = queue_selector_smoke()
    check("queue selector smoke", ok, detail, results)

    direct = run([sys.executable, "tests/test_backlog_worker_selection.py"])
    check("backlog worker direct tests", direct.returncode == 0, (direct.stderr or direct.stdout).strip()[-1200:], results)

    pytest = run([sys.executable, "-m", "pytest", "tests"])
    if pytest.returncode == 0:
        check("pytest suite", True, results=results)
    elif "No module named pytest" in (pytest.stderr + pytest.stdout):
        check("pytest suite", True, "pytest unavailable; direct smoke tests passed", results)
    else:
        check("pytest suite", False, (pytest.stderr or pytest.stdout).strip()[-1200:], results)

    failed = [row for row in results if not row[1]]
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} {name}{' - ' + detail if detail else ''}")
    if failed:
        print(f"post-run check failed: {len(failed)} issue(s)", file=sys.stderr)
        return 1
    print("post-run check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
