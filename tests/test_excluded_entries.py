import importlib.util
from pathlib import Path


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
