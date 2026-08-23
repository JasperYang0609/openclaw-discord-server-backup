import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/audit_discord_inventory_v3.py"
spec = importlib.util.spec_from_file_location("audit_discord_inventory_v3", SCRIPT)
audit = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit)


def test_dedupe_keeps_one_thread_per_id():
    rows = [
        {"id": "2", "name": "second", "parent_id": "10"},
        {"id": "1", "name": "old", "parent_id": "10"},
        {"id": "1", "name": "new", "parent_id": "10"},
    ]
    result = audit.dedupe(rows)
    assert [row["id"] for row in result] == ["1", "2"]
    assert result[0]["name"] == "new"


class FakeClient:
    def __init__(self, responses, errors=None):
        self.responses = responses
        self.errors = errors or set()

    def get(self, path, params=None):
        if path in self.errors:
            raise RuntimeError(f"blocked: {path}")
        return self.responses.get(path, {"threads": [], "has_more": False})


def test_archived_threads_reports_incomplete_when_page_limit_is_exhausted():
    endpoint = "/channels/{channel_id}/threads/archived/public"
    client = FakeClient({
        "/channels/10/threads/archived/public": {
            "threads": [{"id": "2", "thread_metadata": {"archive_timestamp": "2026-08-20T00:00:00Z"}}],
            "has_more": True,
        }
    })
    rows, complete = audit.archived_threads(client, "10", endpoint, page_limit=1)
    assert [row["id"] for row in rows] == ["2"]
    assert complete is False


def test_collect_inventory_splits_active_and_archived_thread_counts():
    client = FakeClient({
        "/guilds/guild/channels": [{"id": "10", "name": "general", "type": 0}],
        "/guilds/guild/threads/active": {"threads": [{"id": "1", "name": "active", "parent_id": "10"}]},
        "/channels/10/threads/archived/public": {
            "threads": [{"id": "2", "name": "archived", "parent_id": "10"}],
            "has_more": False,
        },
    })
    channels, threads, warnings, metrics = audit.collect_inventory(client, "guild", archived_page_limit=2)
    assert len(channels) == 1
    assert {row["id"] for row in threads} == {"1", "2"}
    assert warnings == []
    assert metrics == {
        "activeThreads": 1,
        "archivedThreads": 1,
        "archivedThreadsObserved": 1,
        "archivedEnumerationStatus": "complete",
    }


def test_collect_inventory_marks_archived_count_unknown_on_endpoint_error():
    blocked = "/channels/10/threads/archived/private"
    client = FakeClient(
        {
            "/guilds/guild/channels": [{"id": "10", "name": "general", "type": 0}],
            "/guilds/guild/threads/active": {"threads": []},
        },
        errors={blocked},
    )
    _, _, warnings, metrics = audit.collect_inventory(client, "guild", archived_page_limit=2)
    assert warnings and warnings[0]["channelId"] == "10"
    assert metrics["archivedThreads"] is None
    assert metrics["archivedThreadsObserved"] == 0
    assert metrics["archivedEnumerationStatus"] == "incomplete"


def test_compare_state_finds_missing_and_orphaned_entries():
    state = {
        "entries": {
            "general": {"type": "channel", "channelId": "1", "relativePath": "general"},
            "deleted": {"type": "thread", "channelId": "9", "relativePath": "general/deleted"},
        }
    }
    channels = [{"id": "1", "name": "general", "type": 0}]
    threads = [{"id": "2", "name": "topic", "parent_id": "1", "parentName": "general"}]

    result = audit.compare_state(state, channels, threads)

    assert result["liveEntries"] == 2
    assert [row["id"] for row in result["missingFromState"]] == ["2"]
    assert [row["channelId"] for row in result["orphanedStateEntries"]] == ["9"]


def test_compare_state_uses_parent_name_for_thread_relative_path():
    result = audit.compare_state(
        {"entries": {}},
        [],
        [{"id": "2", "name": "topic", "parent_id": "1", "parentName": "general"}],
    )
    assert result["missingFromState"][0]["relativePath"] == "general/topic"


def test_register_missing_creates_state_and_archive_directories(tmp_path: Path):
    state_path = tmp_path / "memory/state.json"
    root = tmp_path / "archive"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"version": 3, "entries": {}}', encoding="utf-8")
    registered = audit.register_missing(
        state_path,
        root,
        "guild",
        [{"type": "thread", "id": "2", "name": "topic", "parentName": "general", "parentId": "1"}],
    )
    assert registered == [{"key": "general/topic", "channelId": "2", "type": "thread"}]
    state = __import__("json").loads(state_path.read_text())
    assert state["version"] == 3
    assert state["entries"]["general/topic"]["syncStatus"] == "healthy"
    assert (root / "general/topic/raw").is_dir()
    assert (root / "general/topic/legacy_docs").is_dir()


def test_unique_key_preserves_same_name_with_different_id():
    entries = {"general/topic": {"channelId": "1"}}
    assert audit.unique_key(entries, "general/topic", "2") == "general/topic (2)"


def test_mapping_ledger_preserves_existing_path_and_resolves_collision(tmp_path: Path):
    state = {"entries": {
        "custom/classification": {
            "channelId": "1", "type": "channel", "relativePath": "custom/classification"
        },
        "general": {"channelId": "9", "type": "channel", "relativePath": "general"},
    }}
    channels = [
        {"id": "1", "name": "renamed", "type": 0},
        {"id": "2", "name": "general", "type": 0},
    ]

    ledger = audit.build_mapping_ledger(state, channels, [], tmp_path)
    by_id = {row["channelId"]: row for row in ledger["entries"]}

    assert by_id["1"]["decision"] == "preserve"
    assert by_id["1"]["relativePath"] == "custom/classification"
    assert by_id["2"]["decision"] == "register"
    assert by_id["2"]["relativePath"] == "general (2)"
    assert by_id["2"]["collisionResolvedWithStableId"] is True
    assert ledger["applyAllowed"] is True


def test_mapping_ledger_blocks_unsafe_existing_path(tmp_path: Path):
    state = {"entries": {
        "bad": {"channelId": "1", "type": "channel", "relativePath": "../outside"},
    }}
    ledger = audit.build_mapping_ledger(state, [{"id": "1", "name": "bad", "type": 0}], [], tmp_path)
    assert ledger["applyAllowed"] is False
    assert ledger["entries"][0]["decision"] == "blocked"
