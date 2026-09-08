from __future__ import annotations

import importlib.util
import io
import json
import time
import urllib.error
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill/openclaw-discord-server-backup/scripts/run_full_rich_baseline_v3.py"
SPEC = importlib.util.spec_from_file_location("full_rich_baseline_v3", SCRIPT)
assert SPEC and SPEC.loader
baseline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(baseline)


def test_state_entries_rejects_duplicate_nfkc_paths(tmp_path):
    state = {
        "entries": {
            "one": {
                "channelId": "1",
                "relativePath": "Ｆｏｏ",
                "type": "channel",
            },
            "two": {
                "channelId": "2",
                "relativePath": "foo",
                "type": "thread",
            },
        }
    }
    with pytest.raises(baseline.BaselineError, match="state_entry_identity_invalid"):
        baseline.state_entries(state, tmp_path)


def test_root_pointer_exact_readback_rejects_manifest_tamper(tmp_path):
    run_id = "run-1"
    run_root = tmp_path / "runs" / run_id
    run_root.mkdir(parents=True)
    manifest = {
        "schemaVersion": baseline.RUN_MANIFEST_SCHEMA,
        "runId": run_id,
        "entryCount": 0,
        "entries": [],
    }
    manifest_path = run_root / "run-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pointer = baseline.pointer_body(run_id, baseline.file_sha256(manifest_path))
    (tmp_path / "RUN_CURRENT.json").write_text(json.dumps(pointer), encoding="utf-8")

    loaded_pointer, loaded_manifest = baseline.verify_root_pointer(tmp_path)
    assert loaded_pointer == pointer
    assert loaded_manifest == manifest

    manifest_path.write_text(json.dumps({**manifest, "entryCount": 1}), encoding="utf-8")
    with pytest.raises(baseline.BaselineError, match="root_manifest_checksum_mismatch"):
        baseline.verify_root_pointer(tmp_path)


def test_live_inventory_requires_exact_identity_set(monkeypatch):
    monkeypatch.setattr(
        baseline.inventory_api,
        "collect_inventory",
        lambda *_args, **_kwargs: (
            [{"id": "1"}],
            [],
            [],
            {"archivedEnumerationStatus": "complete", "archivedThreadsObserved": 0},
        ),
    )
    expected = [{
        "channelId": "1",
        "relativePath": "one",
        "normalizedRelativePath": "one",
        "type": "channel",
    }]
    result = baseline.live_inventory(object(), "9", expected, archived_page_limit=10)
    assert result["responseCount"] == 1

    with pytest.raises(baseline.BaselineError, match="discord_inventory_identity_mismatch"):
        baseline.live_inventory(
            object(),
            "9",
            [{**expected[0], "channelId": "2"}],
            archived_page_limit=10,
        )


def test_evidence_ttl_uses_remaining_bounded_run_time(monkeypatch):
    monkeypatch.setattr(baseline.time, "monotonic", lambda: 100.0)
    assert baseline.remaining_evidence_ttl(1_000.0) == 900.0
    assert baseline.remaining_evidence_ttl(
        100.0 + baseline.rich.MAX_LIVE_EVIDENCE_TTL_SECONDS + 1,
    ) == baseline.rich.MAX_LIVE_EVIDENCE_TTL_SECONDS
    with pytest.raises(baseline.BaselineError, match="runtime_budget_exhausted"):
        baseline.remaining_evidence_ttl(100.0)


def test_runtime_argument_cannot_outlive_maximum_evidence_ttl(tmp_path):
    args = baseline.build_parser().parse_args([
        "--root", str(tmp_path),
        "--state", str(tmp_path / "state.json"),
        "--queue", str(tmp_path / "queue.json"),
        "--openclaw-config", str(tmp_path / "openclaw.json"),
        "--guild-id", "1476493755426017414",
        "--run-id", "runtime-too-long",
        "--max-runtime-seconds",
        str(int(baseline.rich.MAX_LIVE_EVIDENCE_TTL_SECONDS) + 1),
    ])
    with pytest.raises(baseline.BaselineError, match="runtime_arguments_invalid"):
        baseline.execute(args)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.payload


def test_discord_transport_retries_transient_http_failure(monkeypatch):
    calls = {"count": 0}

    def urlopen(_request, timeout):
        assert timeout == 30
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib.error.HTTPError(
                "https://discord.example.invalid",
                502,
                "bad gateway",
                {},
                io.BytesIO(b""),
            )
        return _Response(b"{}")

    monkeypatch.setattr(baseline.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(baseline.time, "sleep", lambda _seconds: None)
    transport = baseline.DiscordTransport(
        "test-token",
        max_requests=3,
        deadline_monotonic=time.monotonic() + 60,
    )
    assert transport.get("/test") == {}
    assert transport.requests == 2
    assert transport.retries == 1


def test_resume_reference_allows_only_exact_run_owned_generation(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir(mode=0o700)
    legacy = archive / "one" / "legacy.txt"
    legacy.parent.mkdir()
    legacy.write_text("legacy", encoding="utf-8")
    state = tmp_path / "state.json"
    queue = tmp_path / "queue.json"
    state.write_text("{}", encoding="utf-8")
    queue.write_text("{}", encoding="utf-8")
    reference, evidence = baseline.immutable_reference(
        archive, state, queue, "run-1",
    )
    run_root = archive / "runs" / "run-1"
    run_root.mkdir(parents=True, mode=0o700)
    baseline.rich.atomic_json(run_root / "immutable-pre-repair-evidence.json", evidence)
    resumed = archive / "one/generations/full-run-1-0001/data.bin"
    resumed.parent.mkdir(parents=True)
    resumed.write_bytes(b"resumed")
    loaded, _loaded_evidence = baseline.resume_reference(
        archive,
        state,
        queue,
        run_root,
        [{"relativePath": "one"}],
        "run-1",
    )
    assert loaded == reference

    (archive / "unexplained.txt").write_text("drift", encoding="utf-8")
    with pytest.raises(
        baseline.BaselineError,
        match="resume_archive_contains_unexplained_drift",
    ):
        baseline.resume_reference(
            archive,
            state,
            queue,
            run_root,
            [{"relativePath": "one"}],
            "run-1",
        )


def test_resume_reference_rejects_pre_run_file_drift(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir(mode=0o700)
    legacy = archive / "legacy.txt"
    legacy.write_text("legacy", encoding="utf-8")
    state = tmp_path / "state.json"
    queue = tmp_path / "queue.json"
    state.write_text("{}", encoding="utf-8")
    queue.write_text("{}", encoding="utf-8")
    _reference, evidence = baseline.immutable_reference(
        archive, state, queue, "run-1",
    )
    run_root = archive / "runs" / "run-1"
    run_root.mkdir(parents=True, mode=0o700)
    baseline.rich.atomic_json(run_root / "immutable-pre-repair-evidence.json", evidence)
    legacy.write_text("tampered", encoding="utf-8")
    with pytest.raises(baseline.BaselineError, match="resume_immutable_evidence_drift"):
        baseline.resume_reference(
            archive,
            state,
            queue,
            run_root,
            [{"relativePath": "one"}],
            "run-1",
        )
