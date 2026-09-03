import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/weekly_raw_reconcile_v4.py"
spec = importlib.util.spec_from_file_location("weekly_raw_reconcile_v4", SCRIPT)
weekly = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(weekly)


def test_ordered_entries_excludes_invalid_and_scans_report_entry_last():
    state = {"entries": {
        "report": {"channelId": "3", "relativePath": "report", "syncStatus": "healthy"},
        "normal": {"channelId": "2", "relativePath": "normal", "syncStatus": "healthy"},
        "invalid": {"channelId": "1", "relativePath": "invalid", "invalidChannel": True},
    }}

    rows = weekly.ordered_entries(state, "report")

    assert [key for key, _ in rows] == ["normal", "report"]


def test_capture_report_cutoff_uses_latest_message(monkeypatch):
    entries = [("report", {"channelId": "3", "relativePath": "report"})]
    calls = []
    monkeypatch.setattr(
        weekly.worker,
        "discord_messages",
        lambda token, channel_id, *, after, limit: (
            calls.append((token, channel_id, after, limit))
            or [{"id": "20"}]
        ),
    )

    assert weekly.capture_report_cutoff(entries, "report", "token") == "20"
    assert calls == [("token", "3", None, 1)]


def test_scan_freezes_report_entry_and_excludes_newer_raw_and_live_ids(
    tmp_path: Path, monkeypatch
):
    entries = [("report", {"channelId": "3", "relativePath": "report"})]
    monkeypatch.setattr(
        weekly.reconcile,
        "archive_message_ids",
        lambda raw_dir: ({"10": 1, "20": 1, "30": 1}, []),
    )
    monkeypatch.setattr(
        weekly.reconcile,
        "fetch_all_messages",
        lambda token, channel_id, page_limit: [
            {"id": "10"}, {"id": "20"}, {"id": "30"}
        ],
    )

    rows, messages, raw_ids = weekly.scan(
        entries, tmp_path, "token", 100, "report", "20"
    )

    assert [message["id"] for message in messages["report"]] == ["10", "20"]
    assert raw_ids["report"] == {"10", "20"}
    assert rows[0]["liveMessages"] == 2
    assert rows[0]["liveOnly"] == 0
    assert rows[0]["localOnly"] == 0


def test_safe_entry_dir_rejects_path_escape(tmp_path: Path):
    try:
        weekly.safe_entry_dir(tmp_path, "../outside")
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_apply_missing_is_append_only_and_creates_recovery(tmp_path: Path, monkeypatch):
    state_path = tmp_path / "memory/state.json"
    queue_path = tmp_path / "memory/queue.json"
    root = tmp_path / "archive"
    raw = root / "topic/raw"
    raw.mkdir(parents=True)
    raw.joinpath("2026-08-22.md").write_text("old raw\n", encoding="utf-8")
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"entries": {}}', encoding="utf-8")
    queue_path.write_text('{"items": []}', encoding="utf-8")
    entry = {
        "channelId": "1", "relativePath": "topic", "syncStatus": "healthy",
        "lastWrittenMessageId": "100", "lastMessageId": "100",
    }
    state = {"entries": {"topic": entry}}
    queue = {"items": []}
    calls = []
    monkeypatch.setattr(weekly.worker, "append_batch", lambda archive, current, messages, label: calls.append(messages))

    appended, affected = weekly.apply_missing(
        state, queue, [("topic", entry)], root,
        {"topic": [{"id": "101", "timestamp": "2026-08-22T00:00:00Z"}]},
        {"topic": {"100"}}, "2026-08-23", tmp_path / "evidence/pre-repair",
        state_path, queue_path, set(),
    )

    assert appended == 1
    assert affected == ["topic"]
    assert calls[0][0]["id"] == "101"
    assert (tmp_path / "evidence/pre-repair/state.json").is_file()
    assert (tmp_path / "evidence/pre-repair/raw/topic/2026-08-22.md").read_text() == "old raw\n"
    assert entry["lastWrittenMessageId"] == "101"


def test_local_only_classifier_reports_deleted_and_cross_entry_duplicate():
    rows = [{"key": "a"}, {"key": "b"}]
    messages = {"a": [{"id": "10"}], "b": []}
    raw = {"a": {"10", "11", "12"}, "b": {"12"}}

    result = weekly.classify_local_only(rows, messages, raw)

    assert result["counts"]["deleted_from_discord"] == 1
    assert result["counts"]["cross_entry_duplicate"] == 2
    assert result["counts"]["unknown"] == 0
    assert result["liveOnlyMessageIds"] == 0
    assert result["setConservationPass"] is True
