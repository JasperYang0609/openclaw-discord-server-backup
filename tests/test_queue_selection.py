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
