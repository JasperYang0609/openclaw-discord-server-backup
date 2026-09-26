import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts"


def load_module(name: str):
    path = ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


selector = load_module("select_backlog_candidates")
worker = load_module("run_backlog_worker_v3")
audit = load_module("audit_caught_up_v3")
reconcile = load_module("reconcile_raw_archive_v3")


def excluded_entry(**overrides):
    entry = {
        "type": "channel",
        "channelId": "1497025304323948577",
        "relativePath": "invalid-user-id",
        "lastWrittenMessageId": "100",
        "lastMessageId": "100",
        "lastBackup": "2026-08-20",
        "syncStatus": "excluded",
        "backupExcluded": True,
        "invalidChannel": True,
        "consecutiveErrors": 99,
    }
    entry.update(overrides)
    return entry


def test_exclusion_predicate_accepts_each_terminal_marker():
    for entry in (
        {"backupExcluded": True},
        {"invalidChannel": True},
        {"syncStatus": "excluded"},
    ):
        assert selector.entry_is_excluded(entry)
        assert worker.entry_is_excluded(entry)
        assert audit.entry_is_excluded(entry)
        assert reconcile.entry_is_excluded(entry)


def test_worker_invalidates_active_queue_and_never_selects_excluded_entry():
    state = {"entries": {"bad": excluded_entry()}}
    queue = {
        "version": 1,
        "items": [{
            "entryKey": "bad",
            "channelId": "1497025304323948577",
            "status": "retry",
            "attempts": 7,
            "priority": 1,
        }],
    }

    worker.normalize_queue_items(queue, state)

    assert worker.select_candidates(state, queue, 4, "2026-08-23") == []
    assert queue["items"][0]["status"] == "invalid"
    assert queue["items"][0]["attempts"] == 0
    assert queue["items"][0]["retiredReason"] == "state_entry_excluded_or_invalid"


def test_audit_invalidates_excluded_queue_without_probing():
    entries = {"bad": excluded_entry()}
    queue = {"items": [{"entryKey": "bad", "status": "retry", "attempts": 3}]}

    changed = audit.invalidate_excluded_queue(queue, entries)

    assert changed == 1
    assert queue["items"][0]["status"] == "invalid"
    assert queue["items"][0]["attempts"] == 0


def test_audit_reports_active_queue_after_requeue(monkeypatch, tmp_path, capsys):
    state_path = tmp_path / "state.json"
    queue_path = tmp_path / "queue.json"
    state_path.write_text(json.dumps({
        "entries": {
            "topic": {
                "channelId": "1497025304323948577",
                "relativePath": "topic",
                "type": "channel",
                "lastWrittenMessageId": "100",
                "lastMessageId": "100",
                "syncStatus": "healthy",
            }
        }
    }), encoding="utf-8")
    queue_path.write_text(json.dumps({"version": 1, "items": []}), encoding="utf-8")

    monkeypatch.setattr(audit, "load_token", lambda *_args, **_kwargs: "test-token")
    monkeypatch.setattr(audit, "read_after", lambda *_args, **_kwargs: {
        "ok": True,
        "messages": [{"id": "101", "timestamp": "2026-09-26T00:00:00Z"}],
    })
    monkeypatch.setattr(sys, "argv", [
        "audit_caught_up_v3.py",
        "--state", str(state_path),
        "--queue", str(queue_path),
        "--requeue",
    ])

    assert audit.main() == 0
    result = json.loads(capsys.readouterr().out)
    queue = json.loads(queue_path.read_text(encoding="utf-8"))

    assert result["requeued"] is True
    assert result["activeQueue"] == 1
    assert queue["items"][0]["status"] == "queued"
