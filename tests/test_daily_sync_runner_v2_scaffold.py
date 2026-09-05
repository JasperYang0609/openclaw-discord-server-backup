from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill/openclaw-discord-server-backup/scripts/run_daily_sync_v3.py"
SPEC = importlib.util.spec_from_file_location("daily_sync_v2_scaffold", SCRIPT)
daily = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = daily
SPEC.loader.exec_module(daily)


GUILD = "123456789012345678"
CHANNEL = "1490000000000000001"
THREAD = "1490000000000000002"
PARENT = "1490000000000000003"


def inventory_payload() -> dict:
    return {
        "ok": True,
        "checkedAt": "2026-09-05T05:25:00+08:00",
        "guildId": GUILD,
        "remainingMissing": 0,
        "warnings": [],
        "coverage": {"archivedEnumerationStatus": "complete"},
        "channels": [{"id": CHANNEL, "name": "channel"}],
        "threads": [{"id": THREAD, "parent_id": PARENT, "name": "thread"}],
    }


def mapping_payload() -> dict:
    return {
        "schema": "openclaw-discord-inventory-mapping-v1",
        "generatedAt": "2026-09-05T05:25:01+08:00",
        "applyAllowed": True,
        "blockers": [],
        "entries": [
            {
                "channelId": CHANNEL,
                "type": "channel",
                "relativePath": "主頻道",
                "safePath": True,
                "decision": "preserve",
            },
            {
                "channelId": THREAD,
                "type": "thread",
                "relativePath": "主頻道/討論串",
                "safePath": True,
                "decision": "preserve",
            },
        ],
    }


def binding() -> dict:
    return daily.validate_inventory_binding(
        inventory_payload(), mapping_payload(), guild_id=GUILD,
        today="2026-09-05", timezone_name="Asia/Taipei",
    )


def message(message_id: str, *, content: str = "hello") -> dict:
    return {
        "id": message_id,
        "channel_id": CHANNEL,
        "content": content,
    }


def test_inventory_and_mapping_are_bound_by_deterministic_digest():
    first = binding()
    payload = inventory_payload()
    payload["channels"].reverse()
    second = daily.validate_inventory_binding(
        payload, mapping_payload(), guild_id=GUILD,
        today="2026-09-05", timezone_name="Asia/Taipei",
    )
    assert first == second
    assert first["schemaVersion"] == daily.INVENTORY_BINDING_SCHEMA
    assert daily.HASH_RE.fullmatch(first["inventoryDigest"])


@pytest.mark.parametrize(
    ("mutator", "category"),
    [
        (lambda report, _mapping: report.update(checkedAt="2026-09-04T05:25:00+08:00"), "inventory_stale"),
        (lambda report, _mapping: report["warnings"].append({"error": "truncated"}), "inventory_incomplete"),
        (lambda report, _mapping: report.update(guildId="999999999999999999"), "inventory_incomplete"),
        (lambda _report, mapping: mapping["entries"][0].update(relativePath="../escape"), "unsafe_input_path"),
        (lambda _report, mapping: mapping["entries"][0].update(channelId="1490000000000000099"), "inventory_identity_mismatch"),
    ],
)
def test_inventory_gate_fails_closed(mutator, category):
    report = inventory_payload()
    mapping = mapping_payload()
    mutator(report, mapping)
    with pytest.raises(daily.DailySyncError, match=category):
        daily.validate_inventory_binding(
            report, mapping, guild_id=GUILD,
            today="2026-09-05", timezone_name="Asia/Taipei",
        )


def test_entry_binding_requires_exact_type_and_normalized_path():
    result = daily.bind_entry_inventory(
        binding(), channel_id=CHANNEL, relative_path="主頻道", entry_type="channel"
    )
    assert result["schemaVersion"] == daily.ENTRY_BINDING_SCHEMA
    assert daily.HASH_RE.fullmatch(result["entryBindingSha256"])
    with pytest.raises(daily.DailySyncError, match="inventory_identity_mismatch"):
        daily.bind_entry_inventory(
            binding(), channel_id=CHANNEL, relative_path="另一個路徑", entry_type="channel"
        )


def test_candidate_selection_includes_null_cursor_but_excludes_other_owner_and_today():
    state = {
        "entries": {
            "bootstrap": {
                "channelId": CHANNEL,
                "relativePath": "主頻道",
                "type": "channel",
                "syncStatus": "healthy",
                "lastBackup": None,
                "lastWrittenMessageId": None,
            },
            "owned": {
                "channelId": THREAD,
                "relativePath": "主頻道/討論串",
                "type": "thread",
                "syncStatus": "healthy",
                "lastBackup": "2026-09-04",
                "lastWrittenMessageId": "1",
            },
            "done": {
                "channelId": "1490000000000000004",
                "relativePath": "完成",
                "type": "channel",
                "syncStatus": "healthy",
                "lastBackup": "2026-09-05",
                "lastWrittenMessageId": "2",
            },
        }
    }
    queue = {"items": [{"entryKey": "owned", "status": "queued"}]}
    selected = daily.select_candidates(state, queue, today="2026-09-05", max_entries=6)
    assert [key for key, _entry in selected] == ["bootstrap"]


def test_candidate_selection_is_stable_and_bounded():
    entries = {}
    for offset, name in enumerate(("z", "a", "m"), start=1):
        entries[name] = {
            "channelId": str(1490000000000000100 + offset),
            "relativePath": name,
            "type": "channel",
            "syncStatus": "healthy",
            "lastBackup": "2026-09-04",
            "lastWrittenMessageId": str(offset),
        }
    selected = daily.select_candidates(
        {"entries": entries}, {"items": []}, today="2026-09-05", max_entries=2
    )
    assert [key for key, _entry in selected] == ["a", "m"]


def test_queue_reason_is_typed_and_upsert_preserves_attempt_history():
    queue = {
        "items": [{
            "entryKey": "entry", "status": "retry", "attempts": 4,
            "createdAt": "2026-09-01T00:00:00+00:00",
        }]
    }
    entry = {
        "channelId": CHANNEL, "relativePath": "主頻道", "type": "channel",
        "lastWrittenMessageId": "10",
    }
    daily.upsert_queue_item(
        queue, "entry", entry, status="queued", reason="rich_full_rebuild_required"
    )
    assert queue["items"][0]["attempts"] == 4
    assert queue["items"][0]["priority"] == 90
    with pytest.raises(daily.DailySyncError, match="invalid_queue"):
        daily.upsert_queue_item(queue, "entry", entry, status="queued", reason="made_up")


def test_mutable_scan_rotates_and_marks_wrap_cycle():
    ids = ["10", "20", "30"]
    assert daily.plan_mutable_refresh(ids, None) == daily.MutableRefreshPlan("10", "10", False)
    assert daily.plan_mutable_refresh(ids, "10") == daily.MutableRefreshPlan("20", "20", False)
    assert daily.plan_mutable_refresh(ids, "30") == daily.MutableRefreshPlan("10", "10", True)


def test_mutable_scan_rejects_duplicate_or_unsorted_current_ids():
    with pytest.raises(daily.DailySyncError, match="rich_archive_readback_failed"):
        daily.plan_mutable_refresh(["20", "10"], None)
    with pytest.raises(daily.DailySyncError, match="rich_archive_readback_failed"):
        daily.plan_mutable_refresh(["10", "10"], None)


def test_exact_full_page_is_not_terminal_without_following_empty_page():
    calls = []

    def fetch(_token, _channel, *, after, limit, rate_limit_budget):
        calls.append((after, limit, rate_limit_budget))
        return [message(str(int(after) + index + 1)) for index in range(limit)]

    rows, terminal, requests = daily.fetch_new_messages(
        fetch, "token", CHANNEL, "100", page_size=2, max_pages=1,
        max_messages=2, remaining_messages=2, rate_limit_budget={"waited": 0.0},
    )
    assert [row["id"] for row in rows] == ["101", "102"]
    assert terminal is False and requests == 1 and len(calls) == 1


def test_following_empty_page_is_terminal_and_caps_are_honored():
    pages = [[message("101"), message("102")], []]

    def fetch(_token, _channel, *, after, limit, rate_limit_budget):
        assert limit <= 2 and rate_limit_budget is budget
        return pages.pop(0)

    budget = {"waited": 0.0}
    rows, terminal, requests = daily.fetch_new_messages(
        fetch, "token", CHANNEL, "100", page_size=2, max_pages=2,
        max_messages=4, remaining_messages=4, rate_limit_budget=budget,
    )
    assert [row["id"] for row in rows] == ["101", "102"]
    assert terminal is True and requests == 2


def test_mutable_window_filters_to_canonical_ids():
    plan = daily.MutableRefreshPlan("20", "20", False)

    def fetch(_token, _channel, *, around, limit, rate_limit_budget):
        assert around == "20" and limit == 3
        return [message("10"), message("20"), message("99")]

    rows, requests = daily.fetch_mutable_window(
        fetch, "token", CHANNEL, plan, limit=3, canonical_ids=["10", "20", "30"],
        rate_limit_budget={"waited": 0.0},
    )
    assert [row["id"] for row in rows] == ["10", "20"]
    assert requests == 1


def test_conflicting_same_id_payload_fails_closed():
    with pytest.raises(daily.DailySyncError, match="discord_duplicate_conflict"):
        daily.combine_messages([message("10", content="old")], [message("10", content="new")])


def test_response_declared_byte_cap_fails_closed(monkeypatch):
    class Response:
        headers = {"Content-Length": str(daily.MAX_DISCORD_RESPONSE_BYTES + 1)}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(daily.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(daily.DailySyncError, match="discord_response_too_large"):
        daily.discord_messages(
            "secret", CHANNEL, after="100", limit=30,
            rate_limit_budget={"waited": 0.0},
        )


def test_aggregate_rate_limit_wait_cap_fails_closed(monkeypatch):
    def rate_limited(*_args, **_kwargs):
        raise daily.urllib.error.HTTPError(
            "https://discord.invalid", 429, "limited", {}, io.BytesIO(b'{"retry_after":1.0}')
        )

    monkeypatch.setattr(daily.urllib.request, "urlopen", rate_limited)
    monkeypatch.setattr(daily.time, "sleep", lambda _seconds: None)
    budget = {"waited": daily.MAX_429_WAIT_SECONDS - 0.1}
    with pytest.raises(daily.DailySyncError, match="discord_rate_limit_exhausted"):
        daily.discord_messages("secret", CHANNEL, after="100", limit=30, rate_limit_budget=budget)
    assert budget["waited"] == daily.MAX_429_WAIT_SECONDS - 0.1


def test_queue_then_state_fault_keeps_retry_and_old_cursor(tmp_path):
    queue_path = tmp_path / "queue.json"
    state_path = tmp_path / "state.json"
    queue_path.write_text('{"items":[]}\n', encoding="utf-8")
    state_path.write_text('{"cursor":"10"}\n', encoding="utf-8")

    def crash():
        raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        daily.persist_queue_then_state(
            queue_path, {"items": [{"entryKey": "entry", "status": "retry"}]},
            state_path, {"cursor": "20"}, after_queue=crash,
        )
    assert json.loads(queue_path.read_text())["items"][0]["status"] == "retry"
    assert json.loads(state_path.read_text())["cursor"] == "10"
    assert stat_mode(queue_path) == 0o600


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_execute_stops_before_core_adapter_and_preserves_files(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    state_path = tmp_path / "state.json"
    queue_path = tmp_path / "queue.json"
    inventory_path = tmp_path / "inventory.json"
    mapping_path = tmp_path / "mapping.json"
    config_path = tmp_path / "openclaw.json"
    state_path.write_text(json.dumps({"entries": {}}), encoding="utf-8")
    queue_path.write_text(json.dumps({"items": []}), encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory_payload()), encoding="utf-8")
    mapping_path.write_text(json.dumps(mapping_payload()), encoding="utf-8")
    config_path.write_text(json.dumps({}), encoding="utf-8")
    before = {path: path.read_bytes() for path in (state_path, queue_path)}
    args = argparse.Namespace(
        role="daily-sync-1", state=str(state_path), queue=str(queue_path),
        root=str(archive), inventory=str(inventory_path), mapping_ledger=str(mapping_path),
        guild_id=GUILD, today="2026-09-05", timezone="Asia/Taipei",
        openclaw_config=str(config_path), token_env="UNSET_TEST_TOKEN",
        max_entries=6, max_write_entries=4, page_size=30,
        max_pages_per_entry=2, max_messages_per_entry=60,
        max_read_messages=180, mutable_refresh_limit=10,
    )
    with pytest.raises(daily.DailySyncError, match="rich_core_contract_pending"):
        daily.execute(args)
    assert {path: path.read_bytes() for path in before} == before


def test_error_reason_constructor_never_exposes_arbitrary_text():
    assert daily.DailySyncError("private message body").category == "unexpected_error"
