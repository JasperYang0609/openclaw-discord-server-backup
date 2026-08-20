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
