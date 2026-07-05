import importlib.util
import io
import json
import subprocess
import sys
import time
import urllib.error
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/run_backlog_worker_v3.py"
spec = importlib.util.spec_from_file_location("run_backlog_worker_v3_hardening", SCRIPT)
worker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(worker)

RUN_TODAY = "2026-06-09"


def _entry(cursor="100", **overrides):
    entry = {
        "type": "channel",
        "channelId": "1",
        "relativePath": "topic",
        "lastWrittenMessageId": cursor,
        "lastMessageId": cursor,
        "lastBackup": "2026-05-17",
        "syncStatus": "healthy",
    }
    entry.update(overrides)
    return entry


def test_429_retries_are_bounded(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"retry_after": 0}')
        )

    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)

    try:
        worker.discord_messages("token", "1", after="0", limit=100)
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "429" in str(exc)
    assert raised
    # Initial request plus MAX_429_RETRIES retries, then raise.
    assert calls["n"] == worker.MAX_429_RETRIES + 1


def test_save_json_is_atomic_and_load_json_recovers_from_bak(tmp_path):
    state_path = tmp_path / "state.json"
    worker.save_json(state_path, {"entries": {"a": 1}})
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"entries": {"a": 1}}
    assert not (tmp_path / "state.json.tmp").exists()

    bak = tmp_path / "state.json.bak-backlog-v3-20260601_000000"
    bak.write_text(json.dumps({"entries": {"a": "from-bak"}}), encoding="utf-8")
    state_path.write_text("{ this is not json", encoding="utf-8")

    worker.RECOVERED_SOURCES.clear()
    data = worker.load_json(state_path, {})
    assert data == {"entries": {"a": "from-bak"}}
    assert worker.RECOVERED_SOURCES == [{"path": str(state_path), "recoveredFrom": str(bak)}]
    worker.RECOVERED_SOURCES.clear()


def test_save_state_merged_keeps_disk_cursor_monotonic(tmp_path):
    state_path = tmp_path / "state.json"
    # Disk state was advanced by a concurrent daily job.
    worker.save_json(state_path, {"entries": {"topic": _entry("300")}})

    stale = {"entries": {"topic": _entry("250")}}
    worker.save_state_merged(stale, state_path)
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["entries"]["topic"]["lastWrittenMessageId"] == "300"
    assert on_disk["entries"]["topic"]["lastMessageId"] == "300"

    # In-memory progress newer than disk still wins.
    newer = {"entries": {"topic": _entry("400")}}
    worker.save_state_merged(newer, state_path)
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["entries"]["topic"]["lastWrittenMessageId"] == "400"

    # Entries that only exist on disk are preserved.
    worker.save_json(state_path, {"entries": {"topic": _entry("400"), "other": _entry("50")}})
    partial_view = {"entries": {"topic": _entry("400")}}
    worker.save_state_merged(partial_view, state_path)
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert "other" in on_disk["entries"]


def test_upsert_priority_never_downgrades():
    queue = {"version": 1, "items": [{"entryKey": "topic", "cursorMessageId": "100", "status": "queued", "priority": 10}]}
    item = worker.upsert_queue_item(queue, "topic", _entry("100"), "queued", priority=40)
    assert item["priority"] == 10


def test_reactivating_caught_up_item_resets_attempts():
    queue = {"version": 1, "items": [{"entryKey": "topic", "cursorMessageId": "100", "status": "caught_up", "priority": 40, "attempts": 7}]}
    item = worker.upsert_queue_item(queue, "topic", _entry("100"), "queued", reason="healthy_stale_probe")
    assert item["status"] == "queued"
    assert item["attempts"] == 0


def test_normalize_marks_orphan_item_retired():
    state = {"entries": {"topic": _entry("100")}}
    queue = {
        "version": 1,
        "items": [
            {"entryKey": "topic", "cursorMessageId": "100", "status": "queued", "priority": 40},
            {"entryKey": "gone", "cursorMessageId": "50", "status": "queued", "priority": 40},
        ],
    }
    worker.normalize_queue_items(queue, state)
    by_key = {i["entryKey"]: i for i in queue["items"]}
    assert by_key["gone"]["status"] == "retired"
    assert by_key["topic"]["status"] == "queued"
    active = [i for i in queue["items"] if i.get("status") in worker.ACTIVE]
    assert len(active) == 1


def test_bootstrap_probe_at_most_once_per_run_day():
    state = {
        "entries": {
            "empty": {
                "type": "channel",
                "channelId": "9",
                "relativePath": "empty",
                "lastBackup": RUN_TODAY,
                "syncStatus": "healthy",
            }
        }
    }
    queue = {"version": 1, "items": []}
    assert worker.select_candidates(state, queue, 1, RUN_TODAY) == []

    state["entries"]["empty"]["lastBackup"] = "2026-06-08"
    selected = worker.select_candidates(state, queue, 1, RUN_TODAY)
    assert selected and selected[0][2]["reason"] == "bootstrap_needed"


def test_missing_state_file_exits_nonzero(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--state", str(tmp_path / "missing.json"), "--queue", str(tmp_path / "queue.json"), "--root", str(tmp_path), "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "state file not found" in result.stderr


def test_worker_skips_when_lock_is_held(tmp_path):
    state_path = tmp_path / "state.json"
    queue_path = tmp_path / "queue.json"
    worker.save_json(state_path, {"entries": {}})
    worker.save_json(queue_path, {"version": 1, "items": []})

    lock = worker.acquire_lock(state_path.parent / ".channel_backup.lock")
    assert lock is not None
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--state", str(state_path), "--queue", str(queue_path), "--root", str(tmp_path), "--dry-run"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout).get("skipped") == "locked"
    finally:
        lock.close()


def test_audit_warnings_only_count_active_items(tmp_path):
    state_path = tmp_path / "state.json"
    queue_path = tmp_path / "queue.json"
    worker.save_json(state_path, {"entries": {
        "stuck": _entry("10", relativePath="stuck", channelId="11"),
        "done": _entry("20", relativePath="done", channelId="12"),
    }})
    worker.save_json(queue_path, {
        "version": 1,
        "items": [
            {"entryKey": "stuck", "cursorMessageId": "10", "status": "retry", "priority": 40, "attempts": 9},
            {"entryKey": "done", "cursorMessageId": "20", "status": "caught_up", "priority": 40, "attempts": 9},
        ],
    })
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--state", str(state_path), "--queue", str(queue_path), "--root", str(tmp_path), "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    out = json.loads(result.stdout)
    warned = {w["entry"] for w in out["auditWarnings"]}
    assert "stuck" in warned
    assert "done" not in warned


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
