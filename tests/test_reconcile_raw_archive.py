import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/reconcile_raw_archive_v3.py"
spec = importlib.util.spec_from_file_location("reconcile_raw_archive_v3", SCRIPT)
reconcile = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(reconcile)


def test_archive_message_ids_understands_supported_headers(tmp_path: Path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "2026-08-20.md").write_text(
        "\n".join([
            "### 2026-08-20 01:02:03 +0800 — User — id:1539914416244391999",
            "### 2026-08-20T01:02:03Z｜User｜1539914416244392000",
            "### [2026-08-20 01:02:03] User (1539914416244392001)",
            "message_id: 1539914416244392002",
            "ordinary body mentioning 1539914416244392999 must not count",
        ]) + "\n",
        encoding="utf-8",
    )

    ids, files = reconcile.archive_message_ids(raw)

    assert len(files) == 1
    assert set(ids) == {
        "1539914416244391999",
        "1539914416244392000",
        "1539914416244392001",
        "1539914416244392002",
    }


def test_local_row_flags_cursor_ahead_of_raw(tmp_path: Path):
    raw = tmp_path / "topic" / "raw"
    raw.mkdir(parents=True)
    (raw / "2026-08-20.md").write_text(
        "### 2026-08-20 01:02:03 +0800 — User — id:1539914416244391999\n\nhello\n",
        encoding="utf-8",
    )
    entry = {
        "relativePath": "topic",
        "channelId": "1",
        "lastWrittenMessageId": "1539914416244392000",
        "lastMessageId": "1539914416244392000",
    }

    row = reconcile.local_row("topic", entry, tmp_path)

    assert "state_cursor_not_in_raw" in row["issues"]
    assert row["verifiableRawIds"] == 1


def test_local_row_flags_missing_raw(tmp_path: Path):
    entry = {
        "relativePath": "empty",
        "channelId": "1",
        "lastWrittenMessageId": "1539914416244392000",
        "lastMessageId": "1539914416244392000",
    }

    row = reconcile.local_row("empty", entry, tmp_path)

    assert "no_raw_md" in row["issues"]
    assert "state_cursor_not_in_raw" in row["issues"]
