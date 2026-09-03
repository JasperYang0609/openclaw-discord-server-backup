import json
import subprocess
import sys
from pathlib import Path


def test_selector_prefers_queue(tmp_path: Path):
    state = tmp_path / "state.json"
    queue = tmp_path / "queue.json"
    state.write_text(json.dumps({
        "entries": {
            "a": {"type": "channel", "channelId": "1", "relativePath": "a", "lastWrittenMessageId": "10", "lastMessageId": "10", "lastBackup": "2026-05-17", "syncStatus": "healthy"},
            "b": {"type": "channel", "channelId": "2", "relativePath": "b", "lastWrittenMessageId": "20", "lastMessageId": "20", "lastBackup": "2026-05-17", "syncStatus": "partial", "backlogReason": "page_limit_reached"}
        }
    }), encoding="utf-8")
    queue.write_text(json.dumps({"items": [{"entryKey": "b", "status": "queued", "priority": 5, "reason": "page_limit_reached"}]}), encoding="utf-8")
    script = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/select_backlog_candidates.py"
    out = subprocess.check_output([sys.executable, str(script), "--state", str(state), "--queue", str(queue), "--today", "2026-05-17", "--limit", "1"])
    data = json.loads(out)
    assert data["selected"][0]["key"] == "b"


def test_selector_balances_bootstrap_and_stale_without_selecting_excluded(tmp_path: Path):
    state = tmp_path / "state.json"
    queue = tmp_path / "queue.json"
    entries = {
        f"stale-{i}": {
            "type": "channel",
            "channelId": f"s{i}",
            "relativePath": f"stale-{i}",
            "lastWrittenMessageId": str(100 + i),
            "lastMessageId": str(100 + i),
            "lastBackup": "2026-05-17",
            "syncStatus": "healthy",
        }
        for i in range(8)
    }
    entries.update({
        f"new-{i}": {
            "type": "thread",
            "channelId": f"n{i}",
            "relativePath": f"parent/new-{i}",
            "lastBackup": None,
            "syncStatus": "healthy",
        }
        for i in range(4)
    })
    entries["excluded-stale"] = {
        "type": "channel",
        "channelId": "excluded-s",
        "relativePath": "000-excluded-stale",
        "lastWrittenMessageId": "50",
        "lastBackup": "2026-01-01",
        "invalidChannel": True,
    }
    entries["excluded-bootstrap"] = {
        "type": "thread",
        "channelId": "excluded-b",
        "relativePath": "000-excluded-bootstrap",
        "lastBackup": None,
        "syncStatus": "excluded",
    }
    state.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    queue.write_text(json.dumps({"items": []}), encoding="utf-8")
    script = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/select_backlog_candidates.py"

    out = subprocess.check_output([
        sys.executable, str(script), "--state", str(state), "--queue", str(queue),
        "--today", "2026-06-09", "--limit", "4",
    ])
    selected = json.loads(out)["selected"]

    reasons = [item["reason"] for item in selected]
    keys = [item["key"] for item in selected]
    assert reasons.count("bootstrap_needed") == 2
    assert reasons.count("stale_incremental") == 2
    assert "excluded-stale" not in keys
    assert "excluded-bootstrap" not in keys
