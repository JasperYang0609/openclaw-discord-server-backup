from __future__ import annotations

import importlib.util
import json
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
