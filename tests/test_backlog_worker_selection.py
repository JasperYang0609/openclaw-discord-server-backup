import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/run_backlog_worker_v3.py"
spec = importlib.util.spec_from_file_location("run_backlog_worker_v3", SCRIPT)
worker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(worker)


def test_selects_healthy_stale_entry_not_in_queue():
    state = {
        "entries": {
            "quiet": {
                "type": "channel",
                "channelId": "1",
                "relativePath": "quiet",
                "lastWrittenMessageId": "100",
                "lastMessageId": "100",
                "lastBackup": "2026-05-17",
                "syncStatus": "healthy",
            }
        }
    }
    queue = {"version": 1, "items": []}

    selected = worker.select_candidates(state, queue, 1)

    assert selected[0][0] == "quiet"
    item = selected[0][2]
    assert item["status"] == "queued"
    assert item["reason"] == "healthy_stale_probe"
    assert item["cursorMessageId"] == "100"


def test_reactivated_queue_cursor_never_lags_state_cursor():
    state = {
        "entries": {
            "topic": {
                "type": "channel",
                "channelId": "1",
                "relativePath": "topic",
                "lastWrittenMessageId": "200",
                "lastMessageId": "200",
                "lastBackup": "2026-05-17",
                "syncStatus": "partial",
                "backlogReason": "page_limit_reached",
            }
        }
    }
    queue = {
        "version": 1,
        "items": [
            {
                "entryKey": "topic",
                "channelId": "1",
                "relativePath": "topic",
                "cursorMessageId": "150",
                "status": "caught_up",
                "reason": "old",
                "priority": 80,
            }
        ],
    }

    selected = worker.select_candidates(state, queue, 1)

    item = selected[0][2]
    assert item["status"] == "queued"
    assert item["reason"] == "page_limit_reached"
    assert item["priority"] == 40
    assert item["cursorMessageId"] == "200"


def test_active_queue_cursor_is_advanced_to_state_cursor():
    state = {
        "entries": {
            "topic": {
                "type": "channel",
                "channelId": "1",
                "relativePath": "topic",
                "lastWrittenMessageId": "300",
                "lastMessageId": "300",
                "lastBackup": "2026-06-08",
                "syncStatus": "healthy",
            }
        }
    }
    queue = {
        "version": 1,
        "items": [
            {
                "entryKey": "topic",
                "channelId": "1",
                "relativePath": "topic",
                "cursorMessageId": "250",
                "status": "queued",
                "reason": "live_probe_found_new_messages",
                "priority": 10,
            }
        ],
    }

    selected = worker.select_candidates(state, queue, 1)

    assert selected[0][2]["cursorMessageId"] == "300"


def test_stale_probe_only_enqueues_selected_limit():
    state = {
        "entries": {
            f"quiet{i}": {
                "type": "channel",
                "channelId": str(i),
                "relativePath": f"quiet{i}",
                "lastWrittenMessageId": str(100 + i),
                "lastMessageId": str(100 + i),
                "lastBackup": "2026-05-17",
                "syncStatus": "healthy",
            }
            for i in range(5)
        }
    }
    queue = {"version": 1, "items": []}

    selected = worker.select_candidates(state, queue, 2)

    assert len(selected) == 2
    assert len(queue["items"]) == 2
    assert all(item["reason"] == "healthy_stale_probe" for item in queue["items"])


def test_null_cursor_entry_is_selected_for_bounded_bootstrap():
    state = {
        "entries": {
            "new-thread": {
                "type": "thread",
                "channelId": "500",
                "relativePath": "parent/new-thread",
                "lastBackup": None,
                "syncStatus": "healthy",
            }
        }
    }
    queue = {"version": 1, "items": []}

    selected = worker.select_candidates(state, queue, 1)

    assert selected[0][0] == "new-thread"
    item = selected[0][2]
    assert item["reason"] == "bootstrap_needed"
    assert item["priority"] == 90
    assert item["cursorMessageId"] == "0"


def test_normalize_queue_collapses_stale_duplicate_active_item():
    state = {
        "entries": {
            "topic": {
                "type": "thread",
                "channelId": "1",
                "relativePath": "parent/topic",
                "lastWrittenMessageId": "300",
                "lastMessageId": "300",
                "syncStatus": "healthy",
            }
        }
    }
    queue = {
        "version": 1,
        "items": [
            {"entryKey": "topic", "cursorMessageId": "300", "status": "caught_up", "priority": 5, "attempts": 1},
            {"entryKey": "topic", "cursorMessageId": "250", "status": "catching_up", "priority": 80, "attempts": 9},
        ],
    }

    worker.normalize_queue_items(queue, state)

    assert len(queue["items"]) == 1
    assert queue["items"][0]["cursorMessageId"] == "300"
    assert queue["items"][0]["status"] == "caught_up"

if __name__ == "__main__":
    test_selects_healthy_stale_entry_not_in_queue()
    test_reactivated_queue_cursor_never_lags_state_cursor()
    test_active_queue_cursor_is_advanced_to_state_cursor()
    test_stale_probe_only_enqueues_selected_limit()
    test_null_cursor_entry_is_selected_for_bounded_bootstrap()
    test_normalize_queue_collapses_stale_duplicate_active_item()
    print("backlog worker selection tests passed")
