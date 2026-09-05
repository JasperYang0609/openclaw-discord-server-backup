from __future__ import annotations

import argparse
from contextlib import contextmanager
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


def current_snapshot(
    ids: list[str], *, verified_empty: bool = False, channel_id: str = CHANNEL
) -> dict:
    entry = daily.bind_entry_inventory(
        binding(), channel_id=channel_id, relative_path="主頻道", entry_type="channel"
    )
    source = {message_id: "a" * 64 for message_id in ids}
    return {
        "schemaVersion": daily.CURRENT_SNAPSHOT_SCHEMA,
        "entryBindingSha256": entry["entryBindingSha256"],
        "channelId": channel_id,
        "generationId": "generation-20260905",
        "generationSha256": "b" * 64,
        "pointerSha256": "c" * 64,
        "canonicalMessageIds": ids,
        "activeApiSourcePayloadSha256ById": source,
        "localGateStatus": "PASS",
        "fullEvidenceGateStatus": "PASS" if verified_empty else "NOT_PROVIDED",
        "verifiedEmpty": verified_empty,
    }


def execute_fixture(tmp_path: Path) -> tuple[argparse.Namespace, dict[Path, bytes]]:
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
    args = argparse.Namespace(
        role="daily-sync-1", state=str(state_path), queue=str(queue_path),
        root=str(archive), inventory=str(inventory_path), mapping_ledger=str(mapping_path),
        guild_id=GUILD, today="2026-09-05", timezone="Asia/Taipei",
        openclaw_config=str(config_path), token_env="UNSET_TEST_TOKEN",
        max_entries=6, max_write_entries=4, page_size=30,
        max_pages_per_entry=2, max_messages_per_entry=60,
        max_read_messages=180, mutable_refresh_limit=10,
    )
    return args, {path: path.read_bytes() for path in (state_path, queue_path)}


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


def test_verified_empty_head_transport_has_no_cursor_query(monkeypatch):
    captured = {}

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps([message("101")]).encode("utf-8")

    def urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(daily.urllib.request, "urlopen", urlopen)
    rows = daily.discord_messages(
        "secret", CHANNEL, head=True, limit=10,
        rate_limit_budget={"waited": 0.0},
    )
    parsed = daily.urllib.parse.urlparse(captured["url"])
    assert daily.urllib.parse.parse_qs(parsed.query) == {"limit": ["10"]}
    assert captured["timeout"] == 30
    assert [row["id"] for row in rows] == ["101"]


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


def test_queue_then_state_crash_keeps_rich_retry_before_canonical_cursor(tmp_path):
    queue_path = tmp_path / "queue.json"
    state_path = tmp_path / "state.json"
    queue_path.write_text('{"items":[]}\n', encoding="utf-8")
    state_path.write_text(
        json.dumps({"entries": {"entry": {"lastWrittenMessageId": "10"}}}) + "\n",
        encoding="utf-8",
    )

    def crash_after_queue():
        raise RuntimeError("crash-after-queue")

    with pytest.raises(RuntimeError, match="crash-after-queue"):
        daily.persist_queue_then_state(
            queue_path,
            {"items": [{
                "entryKey": "entry", "status": "retry",
                "reason": "rich_incremental_readback_error",
            }]},
            state_path,
            {"entries": {"entry": {"lastWrittenMessageId": "20"}}},
            after_queue=crash_after_queue,
        )
    assert json.loads(queue_path.read_text())["items"][0]["reason"].startswith("rich_")
    assert json.loads(state_path.read_text())["entries"]["entry"]["lastWrittenMessageId"] == "10"


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_execute_stops_before_core_adapter_and_preserves_files(tmp_path):
    args, before = execute_fixture(tmp_path)
    with pytest.raises(daily.DailySyncError, match="rich_core_contract_pending"):
        daily.execute(args)
    assert {path: path.read_bytes() for path in before} == before


def test_execute_acquires_nominal_slot_before_first_mutable_load(tmp_path, monkeypatch):
    args, _before = execute_fixture(tmp_path)
    events: list[str] = []
    original_load = daily.load_json_object

    class LockedSlot(daily.LockedRichSlotV2):
        def begin_incremental(self, request):
            events.append("begin-incremental")
            assert request.schema_version == daily.INCREMENTAL_BEGIN_SCHEMA
            raise daily.DailySyncError("rich_core_contract_pending")

    class Adapter(daily.RichCoreAdapterV2):
        @contextmanager
        def open_slot(self, *, lock_path):
            assert lock_path == Path(args.state).parent / ".channel_backup.lock"
            events.append("lock-entered")
            yield LockedSlot()

    def observed_load(path, category):
        if Path(path) == Path(args.state):
            events.append("load-state")
        elif Path(path) == Path(args.queue):
            events.append("load-queue")
        return original_load(path, category)

    monkeypatch.setattr(daily, "load_json_object", observed_load)
    with pytest.raises(daily.DailySyncError, match="rich_core_contract_pending"):
        daily.execute(args, rich_factory=Adapter())
    assert events.index("lock-entered") < events.index("load-state")
    assert events.index("lock-entered") < events.index("load-queue")
    assert events[-1] == "begin-incremental"


def test_exact_adapter_contract_rejects_duck_typing_and_version_drift():
    class DuckTyped:
        contract = daily.SUPPORTED_RICH_CORE_CONTRACT

        def open_slot(self, **_kwargs):
            raise AssertionError("must not be called")

    class WrongVersion(daily.RichCoreAdapterV2):
        contract = daily.RichCoreContractDescriptor(
            adapter_version="rich-adapter-v999",
            slot_protocol=daily.RICH_SLOT_PROTOCOL,
            incremental_protocol=daily.RICH_INCREMENTAL_PROTOCOL,
            full_snapshot_protocol=daily.RICH_FULL_SNAPSHOT_PROTOCOL,
            root_current_protocol=daily.RICH_ROOT_CURRENT_PROTOCOL,
            operations=daily.RICH_CORE_OPERATIONS,
        )

    with pytest.raises(daily.DailySyncError, match="rich_core_contract_unsupported"):
        daily.require_rich_adapter(DuckTyped())
    with pytest.raises(daily.DailySyncError, match="rich_core_contract_unsupported"):
        daily.require_rich_adapter(WrongVersion())
    assert daily.require_rich_adapter(daily.RichCoreAdapterV2()).contract == daily.SUPPORTED_RICH_CORE_CONTRACT


def test_unsupported_adapter_is_rejected_before_mutable_load(tmp_path, monkeypatch):
    args, _before = execute_fixture(tmp_path)

    class DuckTyped:
        contract = daily.SUPPORTED_RICH_CORE_CONTRACT

        def open_slot(self, **_kwargs):
            raise AssertionError("must not be called")

    monkeypatch.setattr(
        daily,
        "load_json_object",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("mutable load must not happen")
        ),
    )
    with pytest.raises(daily.DailySyncError, match="rich_core_contract_unsupported"):
        daily.execute(args, rich_factory=DuckTyped())


def test_two_phase_incremental_and_full_protocols_fail_closed_by_default(tmp_path):
    locked = daily.LockedRichSlotV2()
    incremental_request = daily.IncrementalBeginRequest(
        schema_version=daily.INCREMENTAL_BEGIN_SCHEMA,
        role="daily-sync-1",
        archive_root=tmp_path,
        inventory_digest="a" * 64,
        inventory_observed_at="2026-09-05T00:00:00+00:00",
        entry_bindings=(),
        limits=(("maxEntries", 6),),
    )
    full_request = daily.FullSnapshotRequest(
        schema_version=daily.FULL_SNAPSHOT_REQUEST_SCHEMA,
        run_id="run-20260905",
        archive_root=tmp_path,
        inventory_digest="a" * 64,
        expected_entry_bindings=(),
    )
    with pytest.raises(daily.DailySyncError, match="rich_core_contract_pending"):
        locked.begin_incremental(incremental_request)
    with pytest.raises(daily.DailySyncError, match="rich_core_contract_pending"):
        locked.begin_full_rebuild(full_request)
    full_session = daily.RichFullRebuildSessionV2()
    for operation in (
        "collect_and_stage_full_snapshot",
        "reserve_full_stage_assets",
        "install_full_pass_evidence",
        "publish_full_entry",
        "finalize_full_rebuild",
    ):
        with pytest.raises(daily.DailySyncError, match="rich_core_contract_pending"):
            getattr(full_session, operation)(full_request)
    root_request = daily.RootRunCurrentRequest(
        schema_version=daily.ROOT_RUN_CURRENT_REQUEST_SCHEMA,
        run_id="run-20260905",
        archive_root=tmp_path,
        inventory_digest="a" * 64,
        expected_entry_generation_sha256={},
    )
    for operation in ("publish_root_run_current", "inspect_root_run_current"):
        with pytest.raises(daily.DailySyncError, match="rich_core_contract_pending"):
            getattr(full_session, operation)(root_request)
    assert daily.SUPPORTED_RICH_CORE_CONTRACT.operations == (
        "open_slot", "begin_incremental", "inspect_current",
        "merge_incremental", "begin_full_rebuild",
        "collect_and_stage_full_snapshot", "reserve_full_stage_assets",
        "install_full_pass_evidence", "publish_full_entry",
        "finalize_full_rebuild", "publish_root_run_current",
        "inspect_root_run_current",
    )


def test_verified_empty_head_read_detects_first_message_on_next_day():
    entry = daily.bind_entry_inventory(
        binding(), channel_id=CHANNEL, relative_path="主頻道", entry_type="channel"
    )
    snapshot = current_snapshot([], verified_empty=True)
    pages = [[], [message("101")]]

    def fetch(_token, _channel, *, head, limit, rate_limit_budget):
        assert head is True and limit == 10 and rate_limit_budget is budget
        return pages.pop(0)

    budget = {"waited": 0.0}
    first, first_complete, first_requests = daily.fetch_verified_empty_head(
        fetch, "token", CHANNEL, snapshot=snapshot, entry_binding=entry,
        limit=10, rate_limit_budget=budget,
    )
    second, second_complete, second_requests = daily.fetch_verified_empty_head(
        fetch, "token", CHANNEL, snapshot=snapshot, entry_binding=entry,
        limit=10, rate_limit_budget=budget,
    )
    assert first == [] and first_complete is True and first_requests == 1
    assert [row["id"] for row in second] == ["101"]
    assert second_complete is True and second_requests == 1


def test_verified_empty_head_exact_limit_is_not_complete():
    entry = daily.bind_entry_inventory(
        binding(), channel_id=CHANNEL, relative_path="主頻道", entry_type="channel"
    )
    snapshot = current_snapshot([], verified_empty=True)

    def fetch(_token, _channel, *, head, limit, rate_limit_budget):
        return [message(str(100 + index)) for index in range(limit)]

    rows, complete, _requests = daily.fetch_verified_empty_head(
        fetch, "token", CHANNEL, snapshot=snapshot, entry_binding=entry,
        limit=2, rate_limit_budget={"waited": 0.0},
    )
    assert len(rows) == 2 and complete is False


def test_quiet_current_rejects_cursor_missing_from_canonical_and_generic_ok():
    entry = daily.bind_entry_inventory(
        binding(), channel_id=CHANNEL, relative_path="主頻道", entry_type="channel"
    )
    with pytest.raises(daily.DailySyncError, match="rich_archive_readback_failed"):
        daily.validate_quiet_current(current_snapshot(["10"]), entry, cursor="20")
    with pytest.raises(daily.DailySyncError, match="rich_archive_readback_failed"):
        daily.validate_quiet_current({"ok": True, "verified": True}, entry, cursor="10")
    valid = daily.validate_quiet_current(current_snapshot(["10", "20"]), entry, cursor="20")
    assert valid["canonicalMessageIds"] == ["10", "20"]


def test_unverified_empty_snapshot_cannot_authorize_head_read():
    entry = daily.bind_entry_inventory(
        binding(), channel_id=CHANNEL, relative_path="主頻道", entry_type="channel"
    )
    with pytest.raises(daily.DailySyncError, match="rich_baseline_missing"):
        daily.fetch_verified_empty_head(
            lambda *_args, **_kwargs: [], "token", CHANNEL,
            snapshot=current_snapshot([], verified_empty=False), entry_binding=entry,
            limit=10, rate_limit_budget={"waited": 0.0},
        )


def test_verified_empty_requires_full_pass_and_current_requires_local_pass():
    entry = daily.bind_entry_inventory(
        binding(), channel_id=CHANNEL, relative_path="主頻道", entry_type="channel"
    )
    missing_full_gate = current_snapshot([], verified_empty=True)
    missing_full_gate["fullEvidenceGateStatus"] = "NOT_PROVIDED"
    with pytest.raises(daily.DailySyncError, match="rich_archive_readback_failed"):
        daily.validate_current_snapshot(missing_full_gate, entry)

    local_failed = current_snapshot(["10"])
    local_failed["localGateStatus"] = "FAIL"
    with pytest.raises(daily.DailySyncError, match="rich_archive_readback_failed"):
        daily.validate_current_snapshot(local_failed, entry)


def test_merge_readback_contract_binds_raw_source_current_and_operation_context():
    entry = daily.bind_entry_inventory(
        binding(), channel_id=CHANNEL, relative_path="主頻道", entry_type="channel"
    )
    ids = ["10", "20"]
    raw_hashes = {message_id: "d" * 64 for message_id in ids}
    commit = {
        "schemaVersion": daily.MERGE_COMMIT_SCHEMA,
        "entryBindingSha256": entry["entryBindingSha256"],
        "channelId": CHANNEL,
        "preCurrentGenerationId": "generation-before",
        "preCurrentGenerationSha256": "e" * 64,
        "committedGenerationId": "generation-20260905",
        "committedGenerationSha256": "b" * 64,
        "currentPointerSha256": "c" * 64,
        "fetchedMessageIds": ids,
        "fetchedRawPayloadSha256ById": raw_hashes,
        "activeApiSourcePayloadSha256ById": {message_id: "a" * 64 for message_id in ids},
        "inventoryDigest": binding()["inventoryDigest"],
        "inventoryObservedAt": binding()["observedAt"],
        "verifiedCutoff": "2026-09-05T00:00:00+00:00",
        "runContextId": "run-context-20260905",
        "lockReceiptSha256": "f" * 64,
        "budgetReceiptSha256": "1" * 64,
        "mode": "incremental",
    }
    valid = daily.validate_merge_readback(
        commit,
        current_snapshot(ids),
        entry_binding=entry,
        expected_raw_sha256_by_id=raw_hashes,
        inventory_digest=binding()["inventoryDigest"],
        inventory_observed_at=binding()["observedAt"],
        verified_cutoff="2026-09-05T00:00:00+00:00",
    )
    assert valid["fetchedMessageIds"] == ids

    missing = dict(commit)
    missing["fetchedMessageIds"] = ["10"]
    with pytest.raises(daily.DailySyncError, match="rich_archive_readback_failed"):
        daily.validate_merge_readback(
            missing,
            current_snapshot(ids),
            entry_binding=entry,
            expected_raw_sha256_by_id=raw_hashes,
            inventory_digest=binding()["inventoryDigest"],
            inventory_observed_at=binding()["observedAt"],
            verified_cutoff="2026-09-05T00:00:00+00:00",
        )

    stale_current = current_snapshot(ids)
    stale_current["pointerSha256"] = "9" * 64
    with pytest.raises(daily.DailySyncError, match="rich_archive_readback_failed"):
        daily.validate_merge_readback(
            commit,
            stale_current,
            entry_binding=entry,
            expected_raw_sha256_by_id=raw_hashes,
            inventory_digest=binding()["inventoryDigest"],
            inventory_observed_at=binding()["observedAt"],
            verified_cutoff="2026-09-05T00:00:00+00:00",
        )


def test_error_reason_constructor_never_exposes_arbitrary_text():
    assert daily.DailySyncError("private message body").category == "unexpected_error"
