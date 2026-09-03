import fcntl
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/check_daily_sync_gate.py"


def run_gate(tmp_path: Path, payload: dict) -> subprocess.CompletedProcess[str]:
    state = tmp_path / "memory/state.json"
    inventory = tmp_path / "inventory.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}\n", encoding="utf-8")
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--state", str(state), "--inventory", str(inventory), "--today", "2026-09-03", "--compact"],
        text=True,
        capture_output=True,
        check=False,
    )


def healthy_inventory() -> dict:
    return {"ok": True, "checkedAt": "2026-09-03T09:00:00+00:00", "remainingMissing": 0, "warnings": []}


def test_gate_accepts_current_complete_inventory(tmp_path: Path) -> None:
    result = run_gate(tmp_path, healthy_inventory())
    assert result.returncode == 0
    assert "ok=true" in result.stdout


def test_gate_rejects_stale_inventory(tmp_path: Path) -> None:
    payload = healthy_inventory()
    payload["checkedAt"] = "2026-09-02T09:00:00+00:00"
    result = run_gate(tmp_path, payload)
    assert result.returncode == 2
    assert "inventory_stale" in result.stdout


def test_gate_rejects_active_backup_lock(tmp_path: Path) -> None:
    state = tmp_path / "memory/state.json"
    inventory = tmp_path / "inventory.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}\n", encoding="utf-8")
    inventory.write_text(json.dumps(healthy_inventory()), encoding="utf-8")
    lock = (state.parent / ".channel_backup.lock").open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--state", str(state), "--inventory", str(inventory), "--today", "2026-09-03", "--compact"],
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    assert result.returncode == 2
    assert "backup_lock_busy" in result.stdout
