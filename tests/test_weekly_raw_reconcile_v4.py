import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


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


def test_weekly_worker_renderer_preserves_component_only_status_message():
    rendered = weekly.worker.fmt_raw({
        "id": "1552571944937721877",
        "timestamp": "2026-09-24T06:47:00+00:00",
        "content": "",
        "author": {"username": "小萊"},
        "attachments": [],
        "components": [{
            "type": 17,
            "components": [{"type": 10, "content": "狀態回報窗正文"}],
        }],
    })

    assert "狀態回報窗正文" in rendered
    assert "discord-component-payload-ref:1552571944937721877" in rendered


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


def test_freeze_scope_cutoffs_covers_every_entry_and_preserves_existing_cutoff():
    entries = [
        ("active", {"channelId": "1"}),
        ("raw-only", {"channelId": "2"}),
        ("empty", {"channelId": "3"}),
        ("report", {"channelId": "4"}),
    ]

    cutoffs = weekly.freeze_scope_cutoffs(
        entries,
        {
            "active": [{"id": "10"}, {"id": "30"}],
            "report": [{"id": "999"}],
        },
        {
            "active": {"20"},
            "raw-only": {"40"},
            "empty": set(),
            "report": {"90"},
        },
        {"report": "25"},
    )

    assert cutoffs == {
        "active": "30",
        "raw-only": "40",
        "empty": "0",
        "report": "25",
    }


def test_scan_applies_frozen_cutoff_to_every_entry(tmp_path: Path, monkeypatch):
    entries = [
        ("first", {"channelId": "1", "relativePath": "first"}),
        ("second", {"channelId": "2", "relativePath": "second"}),
    ]
    monkeypatch.setattr(
        weekly.reconcile,
        "archive_message_ids",
        lambda raw_dir: ({"10": 1, "20": 1}, []),
    )
    monkeypatch.setattr(
        weekly.reconcile,
        "fetch_all_messages",
        lambda token, channel_id, page_limit: [{"id": "10"}, {"id": "20"}],
    )

    rows, messages, raw_ids = weekly.scan(
        entries,
        tmp_path,
        "token",
        100,
        cutoff_message_ids={"first": "10", "second": "10"},
    )

    assert [row["liveMessages"] for row in rows] == [1, 1]
    assert {key: [message["id"] for message in value] for key, value in messages.items()} == {
        "first": ["10"],
        "second": ["10"],
    }
    assert raw_ids == {"first": {"10"}, "second": {"10"}}


def test_scan_keeps_only_exception_class_from_discord_read_failure(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        weekly.reconcile,
        "archive_message_ids",
        lambda _raw_dir: ({"10": 1}, []),
    )

    def fail_read(*_args, **_kwargs):
        raise TimeoutError("private Discord response body")

    monkeypatch.setattr(weekly.reconcile, "fetch_all_messages", fail_read)

    rows, messages, raw_ids = weekly.scan(
        [("flaky", {"channelId": "2", "relativePath": "flaky"})],
        tmp_path,
        "token",
        100,
    )

    assert rows[0]["liveError"] == "TimeoutError"
    assert "private Discord response body" not in json.dumps(rows)
    assert messages == {}
    assert raw_ids == {"flaky": {"10"}}


def run_final_retry_scenario(
    tmp_path: Path,
    monkeypatch,
    *,
    retry_row: dict,
) -> tuple[int, dict, list[list[str]], list[list[str]]]:
    state_path = tmp_path / "memory/state.json"
    queue_path = tmp_path / "memory/queue.json"
    root = tmp_path / "archive"
    evidence = tmp_path / "evidence"
    state_path.parent.mkdir(parents=True)
    root.mkdir()
    state_path.write_text(json.dumps({
        "entries": {
            "healthy": {"channelId": "1", "relativePath": "healthy"},
            "flaky": {"channelId": "2", "relativePath": "flaky"},
        }
    }), encoding="utf-8")
    queue_path.write_text('{"items": []}', encoding="utf-8")

    scan_calls: list[list[str]] = []
    responses = [
        (
            [
                {"key": "healthy", "channelId": "1", "liveMessages": 1, "liveOnly": 0},
                {"key": "flaky", "channelId": "2", "liveMessages": 1, "liveOnly": 1},
            ],
            {"healthy": [{"id": "10"}], "flaky": [{"id": "20"}]},
            {"healthy": {"10"}, "flaky": set()},
        ),
        (
            [
                {"key": "healthy", "channelId": "1", "liveMessages": 1, "liveOnly": 0},
                {"key": "flaky", "channelId": "2", "liveError": "TimeoutError: private response body"},
            ],
            {"healthy": [{"id": "10"}]},
            {"healthy": {"10"}, "flaky": {"20"}},
        ),
        (
            [retry_row],
            {"flaky": [{"id": "20"}]} if not retry_row.get("liveError") else {},
            {"flaky": {"20"}},
        ),
    ]

    def fake_scan(entries, *_args, **_kwargs):
        scan_calls.append([key for key, _entry in entries])
        return responses.pop(0)

    apply_calls: list[list[str]] = []

    def fake_apply_missing(
        _state, _queue, entries, _root, _messages, _raw_ids, _today,
        _recovery, _state_path, _queue_path, _copied,
    ):
        apply_calls.append([key for key, _entry in entries])
        return 1, ["flaky"]

    monkeypatch.setattr(weekly, "scan", fake_scan)
    monkeypatch.setattr(weekly, "apply_missing", fake_apply_missing)
    monkeypatch.setattr(weekly.worker, "load_discord_token", lambda *_args: "token")
    monkeypatch.setattr(weekly, "capture_report_cutoff", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "weekly_raw_reconcile_v4.py",
            "--state", str(state_path),
            "--queue", str(queue_path),
            "--root", str(root),
            "--today", "2026-09-20",
            "--evidence-dir", str(evidence),
            "--max-closeout-passes", "1",
        ],
    )

    exit_code = weekly.main()
    summary = json.loads(
        (evidence / "weekly-reconciliation-summary.json").read_text(encoding="utf-8")
    )
    return exit_code, summary, scan_calls, apply_calls


def test_final_only_transient_retries_target_entry_without_duplicate_append(
    tmp_path: Path, monkeypatch
):
    exit_code, summary, scan_calls, apply_calls = run_final_retry_scenario(
        tmp_path,
        monkeypatch,
        retry_row={
            "key": "flaky",
            "channelId": "2",
            "liveMessages": 1,
            "liveOnly": 0,
            "localOnly": 0,
        },
    )

    assert exit_code == 0
    assert scan_calls == [["flaky", "healthy"], ["flaky", "healthy"], ["flaky"]]
    assert apply_calls == [["flaky", "healthy"]]
    assert summary["appended"] == 1
    assert summary["finalLiveErrors"] == 0
    assert summary["finalReadRetry"] == {
        "attemptedEntries": 1,
        "recoveredEntries": 1,
        "exhaustedEntries": 0,
    }
    assert summary["finalReadErrors"] == []
    assert summary["localOnlyClassification"]["setConservationPass"] is True


def test_persistent_final_read_error_fails_closed_with_redacted_receipt(
    tmp_path: Path, monkeypatch
):
    exit_code, summary, scan_calls, apply_calls = run_final_retry_scenario(
        tmp_path,
        monkeypatch,
        retry_row={
            "key": "flaky",
            "channelId": "2",
            "liveError": "TimeoutError: another private response body",
        },
    )

    assert exit_code == 2
    assert scan_calls == [["flaky", "healthy"], ["flaky", "healthy"], ["flaky"]]
    assert apply_calls == [["flaky", "healthy"]]
    assert summary["appended"] == 1
    assert summary["finalLiveErrors"] == 1
    assert summary["finalReadRetry"] == {
        "attemptedEntries": 1,
        "recoveredEntries": 0,
        "exhaustedEntries": 1,
    }
    assert summary["finalReadErrors"] == [{
        "entry": "flaky",
        "channelId": "2",
        "errorClass": "TimeoutError",
    }]
    assert set(summary["finalReadErrors"][0]) == {"entry", "channelId", "errorClass"}
    assert "private response body" not in json.dumps(summary)
    assert summary["localOnlyClassification"]["setConservationPass"] is True


def test_safe_entry_dir_rejects_path_escape(tmp_path: Path):
    try:
        weekly.safe_entry_dir(tmp_path, "../outside")
        raised = False
    except RuntimeError:
        raised = True
    assert raised


@pytest.mark.parametrize("relative_path", ["topic/../other", "/absolute", "topic\\other"])
def test_safe_entry_dir_rejects_any_lexical_traversal(
    tmp_path: Path, relative_path: str
):
    with pytest.raises(RuntimeError):
        weekly.safe_entry_dir(tmp_path, relative_path)


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
    manifest = weekly.verify_evidence_bundle(tmp_path / "evidence/pre-repair")
    assert manifest["schema"] == weekly.EVIDENCE_SCHEMA
    assert {row["path"] for row in manifest["files"]} == {
        "queue.json", "raw/topic/2026-08-22.md", "state.json"
    }
    assert entry["lastWrittenMessageId"] == "101"


def test_apply_missing_backfills_component_only_body_for_existing_raw_id(tmp_path: Path):
    message_id = "1552571944937721877"
    state_path = tmp_path / "memory/state.json"
    queue_path = tmp_path / "memory/queue.json"
    root = tmp_path / "archive"
    raw = root / "topic/raw/2026-09-24.md"
    raw.parent.mkdir(parents=True)
    raw.write_text(
        f"### 2026-09-24 14:47:00 +0800 — 小萊 — id:{message_id}\n\n(無文字內容)\n",
        encoding="utf-8",
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"entries": {}}', encoding="utf-8")
    queue_path.write_text('{"items": []}', encoding="utf-8")
    entry = {
        "channelId": "1",
        "relativePath": "topic",
        "syncStatus": "healthy",
        "lastWrittenMessageId": message_id,
        "lastMessageId": message_id,
    }
    message = {
        "id": message_id,
        "timestamp": "2026-09-24T06:47:00+00:00",
        "content": "",
        "author": {"username": "小萊"},
        "attachments": [],
        "components": [{"type": 10, "content": "回補後的狀態回報正文"}],
    }

    appended, affected = weekly.apply_missing(
        {"entries": {"topic": entry}},
        {"items": []},
        [("topic", entry)],
        root,
        {"topic": [message]},
        {"topic": {message_id}},
        "2026-09-24",
        tmp_path / "evidence/pre-repair",
        state_path,
        queue_path,
        set(),
    )

    assert appended == 0
    assert affected == ["topic"]
    text = raw.read_text(encoding="utf-8")
    assert "回補後的狀態回報正文" in text
    assert text.count(f"discord-component-payload-ref:{message_id}") == 1
    assert (tmp_path / "evidence/pre-repair/evidence-manifest.json").is_file()


def evidence_fixture(tmp_path: Path):
    state_path = tmp_path / "memory/state.json"
    queue_path = tmp_path / "memory/queue.json"
    archive = tmp_path / "archive"
    raw = archive / "team/topic/raw"
    raw.mkdir(parents=True)
    (raw / "2026-09-04.md").write_text("raw evidence\n", encoding="utf-8")
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"entries":{"team/topic":{}}}\n', encoding="utf-8")
    queue_path.write_text('{"items":[]}\n', encoding="utf-8")
    entry = {"channelId": "1", "relativePath": "team/topic"}
    evidence = tmp_path / "evidence/pre-repair"
    weekly.create_pre_repair_evidence(
        evidence, state_path, queue_path, archive, [("team/topic", entry)]
    )
    return evidence, state_path, queue_path, archive, entry


def mutate_evidence(evidence: Path, mutation: str) -> None:
    raw_file = evidence / "raw/team%2Ftopic/2026-09-04.md"
    if mutation == "tamper":
        raw_file.chmod(0o600)
        raw_file.write_text("changed\n", encoding="utf-8")
        raw_file.chmod(0o400)
    elif mutation == "writable":
        raw_file.chmod(0o600)
    elif mutation == "hardlink":
        os.link(raw_file, evidence.parent / "external-hardlink")
    elif mutation == "traversal":
        manifest_path = evidence / "evidence-manifest.json"
        manifest_path.chmod(0o600)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../outside"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o400)
    else:
        raw_file.parent.chmod(0o700)
        if mutation == "missing":
            raw_file.unlink()
        elif mutation == "extra":
            extra = raw_file.parent / "extra.md"
            extra.write_text("extra\n", encoding="utf-8")
            extra.chmod(0o400)
        elif mutation == "symlink":
            raw_file.unlink()
            raw_file.symlink_to(evidence / "state.json")
        elif mutation == "special":
            raw_file.unlink()
            os.mkfifo(raw_file)
            raw_file.chmod(0o400)
        else:
            raise AssertionError(f"unknown mutation: {mutation}")
        raw_file.parent.chmod(0o500)


@pytest.mark.parametrize(
    "mutation", ["tamper", "missing", "extra", "symlink", "special", "writable", "hardlink"]
)
def test_evidence_verifier_rejects_inventory_or_integrity_changes(
    tmp_path: Path, mutation: str
):
    evidence, *_rest = evidence_fixture(tmp_path)
    mutate_evidence(evidence, mutation)

    with pytest.raises(RuntimeError):
        weekly.verify_evidence_bundle(evidence)


@pytest.mark.parametrize("unsafe", ["../outside", "/absolute", "raw\\outside"])
def test_evidence_verifier_rejects_manifest_path_traversal(
    tmp_path: Path, unsafe: str
):
    evidence, *_rest = evidence_fixture(tmp_path)
    manifest_path = evidence / "evidence-manifest.json"
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = unsafe
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o400)

    with pytest.raises(RuntimeError):
        weekly.verify_evidence_bundle(evidence)


def test_evidence_creation_is_atomic_and_refuses_symlink_source(tmp_path: Path):
    state_path = tmp_path / "memory/state.json"
    queue_path = tmp_path / "memory/queue.json"
    archive = tmp_path / "archive"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")
    queue_path.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "raw").mkdir()
    (archive / "topic").parent.mkdir(parents=True)
    (archive / "topic").symlink_to(outside, target_is_directory=True)
    evidence = tmp_path / "evidence/pre-repair"

    with pytest.raises(RuntimeError):
        weekly.create_pre_repair_evidence(
            evidence,
            state_path,
            queue_path,
            archive,
            [("topic", {"relativePath": "topic"})],
        )

    assert not evidence.exists()
    assert list(evidence.parent.glob(".pre-repair-stage-*")) == []


def test_evidence_creation_rejects_hardlinked_source(tmp_path: Path):
    state_path = tmp_path / "memory/state.json"
    queue_path = tmp_path / "memory/queue.json"
    archive = tmp_path / "archive"
    (archive / "topic/raw").mkdir(parents=True)
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")
    queue_path.write_text("{}", encoding="utf-8")
    os.link(state_path, tmp_path / "state-hardlink")
    with pytest.raises(RuntimeError, match="single-link"):
        weekly.create_pre_repair_evidence(
            tmp_path / "evidence/pre-repair", state_path, queue_path, archive,
            [("topic", {"relativePath": "topic"})],
        )


def test_atomic_json_ignores_predictable_symlink_temp_attack(tmp_path: Path):
    target = tmp_path / "summary.json"
    outside = tmp_path / "outside.json"
    outside.write_text("do not change", encoding="utf-8")
    predictable = tmp_path / "summary.json.tmp-weekly-v4"
    predictable.symlink_to(outside)
    weekly.atomic_json(target, {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert outside.read_text(encoding="utf-8") == "do not change"


@pytest.mark.parametrize(
    "mutation", ["tamper", "missing", "extra", "symlink", "special", "writable", "hardlink", "traversal"]
)
def test_apply_missing_blocks_append_when_existing_evidence_is_invalid(
    tmp_path: Path, monkeypatch, mutation: str
):
    evidence, state_path, queue_path, archive, entry = evidence_fixture(tmp_path)
    mutate_evidence(evidence, mutation)
    calls = []
    monkeypatch.setattr(
        weekly.worker,
        "append_batch",
        lambda *args: calls.append(args),
    )
    state = {"entries": {"team/topic": entry}}
    queue = {"items": []}

    with pytest.raises(RuntimeError):
        weekly.apply_missing(
            state,
            queue,
            [("team/topic", entry)],
            archive,
            {"team/topic": [{"id": "101", "timestamp": "2026-09-04T00:00:00Z"}]},
            {"team/topic": set()},
            "2026-09-04",
            evidence,
            state_path,
            queue_path,
            {"team/topic"},
        )

    assert calls == []


def test_verify_evidence_cli_needs_no_discord_or_operational_arguments(
    tmp_path: Path, capsys, monkeypatch
):
    evidence, *_rest = evidence_fixture(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["weekly_raw_reconcile_v4.py", "--verify-evidence", str(evidence)],
    )

    assert weekly.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["schema"] == weekly.EVIDENCE_SCHEMA


def test_evidence_bundle_is_hardened_and_writable_bit_is_rejected(tmp_path: Path):
    evidence, *_rest = evidence_fixture(tmp_path)
    assert evidence.stat().st_mode & 0o222 == 0
    assert all(
        path.lstat().st_mode & 0o222 == 0
        for path in evidence.rglob("*")
        if not path.is_symlink()
    )

    manifest = evidence / "evidence-manifest.json"
    manifest.chmod(0o600)
    with pytest.raises(RuntimeError, match="non-writable"):
        weekly.verify_evidence_bundle(evidence)


def test_existing_evidence_bundle_is_never_overwritten(tmp_path: Path):
    evidence, state_path, queue_path, archive, entry = evidence_fixture(tmp_path)
    before = (evidence / "evidence-manifest.json").read_bytes()

    with pytest.raises(FileExistsError):
        weekly.create_pre_repair_evidence(
            evidence,
            state_path,
            queue_path,
            archive,
            [("team/topic", entry)],
        )

    assert (evidence / "evidence-manifest.json").read_bytes() == before


def test_missing_queue_blocks_evidence_and_append(tmp_path: Path, monkeypatch):
    state_path = tmp_path / "memory/state.json"
    queue_path = tmp_path / "memory/missing-queue.json"
    archive = tmp_path / "archive"
    (archive / "topic/raw").mkdir(parents=True)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"entries":{}}', encoding="utf-8")
    entry = {"channelId": "1", "relativePath": "topic"}
    calls = []
    monkeypatch.setattr(weekly.worker, "append_batch", lambda *args: calls.append(args))

    with pytest.raises(RuntimeError, match="queue.json"):
        weekly.apply_missing(
            {"entries": {"topic": entry}},
            {"items": []},
            [("topic", entry)],
            archive,
            {"topic": [{"id": "101", "timestamp": "2026-09-04T00:00:00Z"}]},
            {"topic": set()},
            "2026-09-04",
            tmp_path / "evidence/pre-repair",
            state_path,
            queue_path,
            set(),
        )

    assert calls == []
    assert not (tmp_path / "evidence/pre-repair").exists()


def test_evidence_verifier_rejects_traversing_entry_source_metadata(tmp_path: Path):
    evidence, *_rest = evidence_fixture(tmp_path)
    manifest_path = evidence / "evidence-manifest.json"
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["sourceRelativePath"] = "../outside"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o400)

    with pytest.raises(RuntimeError):
        weekly.verify_evidence_bundle(evidence)


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
