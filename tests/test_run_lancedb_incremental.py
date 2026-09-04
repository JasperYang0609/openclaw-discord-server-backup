from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill/openclaw-discord-server-backup/scripts/run_lancedb_incremental.py"


def run(tmp_path: Path, command: object, *, dry_run: bool = False) -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / "workspace"
    project = workspace / "knowledge-lancedb"
    project.mkdir(parents=True)
    config = workspace / "config.json"
    config.write_text(json.dumps({
        "lancedb": {
            "enabled": True,
            "projectPath": "knowledge-lancedb",
            "incrementalCommand": command,
            "latestManifest": "knowledge-lancedb/latest.json",
        }
    }), encoding="utf-8")
    argv = [sys.executable, str(SCRIPT), "--config", str(config), "--workspace", str(workspace)]
    if dry_run:
        argv.append("--dry-run")
    return subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)


def test_command_string_is_split_without_shell_execution(tmp_path: Path):
    marker = tmp_path / "shell-injection-marker"
    command = f"{sys.executable} -c 'print(\"INDEX_OK\")' ; touch {marker}"
    proc = run(tmp_path, command)
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()


def test_command_array_runs_as_fixed_argv(tmp_path: Path):
    proc = run(tmp_path, [sys.executable, "-c", "print('INDEX_OK')"])
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    assert result["command"] == [sys.executable, "-c", "print('INDEX_OK')"]
    assert "INDEX_OK" in result["outputTail"]


def test_invalid_command_type_is_rejected(tmp_path: Path):
    proc = run(tmp_path, {"executable": "npm"}, dry_run=True)
    assert proc.returncode != 0
    assert "incrementalCommand" in proc.stderr
