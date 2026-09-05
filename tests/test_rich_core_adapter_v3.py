import copy
import importlib.util
import json
import pickle
import sys
import time
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts"
ADAPTER_SCRIPT = SCRIPTS / "rich_core_adapter_v3.py"


def load_adapter_module():
    spec = importlib.util.spec_from_file_location("rich_core_adapter_v3_test", ADAPTER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def message(message_id="1540000000000000001", *, content="hello"):
    return {
        "id": message_id,
        "channel_id": "1490000000000000001",
        "timestamp": "2026-09-05T03:00:00.000000+00:00",
        "edited_timestamp": None,
        "type": 0,
        "content": content,
        "author": {"id": "1", "username": "jasper", "global_name": "Jasper"},
        "mentions": [],
        "mention_roles": [],
        "mention_everyone": False,
        "attachments": [],
        "embeds": [],
        "components": [],
        "sticker_items": [],
        "pinned": False,
        "tts": False,
        "flags": 0,
        "reactions": [],
    }


def binding(adapter, *, relative_path="test/entry"):
    body = {
        "schemaVersion": "openclaw-discord-daily-entry-binding.v2",
        "guildId": "1476493755426017414",
        "channelId": "1490000000000000001",
        "type": "channel",
        "relativePath": relative_path,
        "normalizedRelativePath": relative_path.casefold(),
        "inventoryDigest": "a" * 64,
        "inventoryObservedAt": "2026-09-05T03:00:00+00:00",
    }
    body["entryBindingSha256"] = adapter.json_sha256(body)
    return adapter.EntryBindingV3.from_mapping(body)


def seed_initial(adapter, archive_root, entry, source=None):
    core = adapter._load_verified_core()
    entry_root = archive_root / entry.relative_path
    store = core.RichArchiveStore(
        entry_root, lock_path=archive_root / ".channel_backup.lock"
    )
    with store.acquire_lock() as token:
        run = core.begin_incremental_run(
            entries=[{
                "channelId": entry.channel_id,
                "relativePath": entry.relative_path,
                "normalizedRelativePath": entry.normalized_relative_path,
            }],
            archive_root=archive_root,
            lock_token=token,
        )
        try:
            stage = store.create_stage(
                "initial",
                copy_current=False,
                lock_token=token,
                run_context=run,
            )
            rows = []
            if source is not None:
                rows = [
                    core.normalize_message(
                        source,
                        expected_channel_id=entry.channel_id,
                        observed_at="2026-09-05T04:00:00+00:00",
                    )
                ]
                core.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", rows)
                core._atomic_bytes(
                    stage / "raw/2026-09-05.md", core.render_day(rows).encode("utf-8")
                )
            core.atomic_json(
                stage / "receipts/rich-archive-latest.json",
                {
                    "schemaVersion": core.ENTRY_RECEIPT_SCHEMA,
                    "gateStatus": "INCOMPLETE",
                    "reason": "test",
                },
            )
            manifest = core.generation_inventory(stage)
            core.atomic_json(stage / "generation-manifest.json", manifest)
            store.publish_stage(
                stage,
                "initial",
                manifest["generationSha256"],
                lock_token=token,
                run_context=run,
            )
        finally:
            run.close()


def write_managed_config(adapter, archive_root):
    state_path = archive_root / "managed-state.json"
    queue_path = archive_root / "managed-queue.json"
    openclaw_path = archive_root / "managed-openclaw.json"
    config_path = archive_root / "managed-backup-config.json"
    state_path.write_text("{}", encoding="utf-8")
    queue_path.write_text("{}", encoding="utf-8")
    openclaw_path.write_text("{}", encoding="utf-8")
    payload = {
        "guildId": "1476493755426017414",
        "backupRoot": str(archive_root),
        "statePath": str(state_path.relative_to(archive_root)),
        "queuePath": str(queue_path.relative_to(archive_root)),
        "openclawConfig": str(openclaw_path),
        "timezone": "Asia/Taipei",
        "limits": {
            "dailyEntryLimit": 6,
            "dailyMessageLimit": 60,
            "dailyMutableRefreshLimit": 10,
        },
        "configSha256": "f" * 64,
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    for path in (state_path, queue_path, openclaw_path, config_path):
        path.chmod(0o600)
    return {
        "workspace": archive_root,
        "config": config_path,
        "state": state_path,
        "queue": queue_path,
        "openclaw": openclaw_path,
        "actualSha256": adapter.file_sha256(config_path),
    }


def begin_session(adapter, tmp_path, entry):
    config = write_managed_config(adapter, tmp_path)
    runtime = adapter.ManagedRichCoreAdapterV3()
    slot_manager = runtime.open_slot(
        archive_root=tmp_path,
        lock_path=tmp_path / ".channel_backup.lock",
        workspace_root=config["workspace"],
        config_path=config["config"],
    )
    slot = slot_manager.__enter__()
    request = adapter.IncrementalBeginRequestV3(
        schema_version=adapter.INCREMENTAL_BEGIN_SCHEMA,
        role="daily-sync-1",
        archive_root=tmp_path,
        workspace_root=config["workspace"],
        state_path=config["state"],
        queue_path=config["queue"],
        openclaw_config_path=config["openclaw"],
        timezone_name="Asia/Taipei",
        inventory_digest=entry.inventory_digest,
        inventory_observed_at=entry.inventory_observed_at,
        entry_bindings=(entry,),
        limits=(
            ("maxEntries", 6),
            ("maxWriteEntries", 4),
            ("maxReadMessages", 180),
            ("maxMessagesPerEntry", 60),
            ("mutableRefreshLimit", 10),
        ),
    )
    session_manager = slot.begin_incremental(request)
    session = session_manager.__enter__()
    return slot_manager, session_manager, session


def close_session(slot_manager, session_manager):
    session_manager.__exit__(None, None, None)
    slot_manager.__exit__(None, None, None)


def test_adapter_v3_exports_exact_contract_and_rejects_alternate_lock(tmp_path):
    adapter = load_adapter_module()
    runtime = adapter.ManagedRichCoreAdapterV3()
    config = write_managed_config(adapter, tmp_path)
    assert runtime.contract == adapter.ADAPTER_CONTRACT
    with pytest.raises(adapter.RichCoreAdapterError) as caught:
        runtime.open_slot(
            archive_root=tmp_path,
            lock_path=tmp_path / "legacy-state-parent.lock",
            workspace_root=config["workspace"],
            config_path=config["config"],
        ).__enter__()
    assert caught.value.category == "rich_core_authority_invalid"
    assert not (tmp_path / "legacy-state-parent.lock").exists()
    assert not (tmp_path / ".channel_backup.lock").exists()


def test_managed_config_hash_is_derived_and_change_revokes_live_slot(tmp_path):
    adapter = load_adapter_module()
    config = write_managed_config(adapter, tmp_path)
    runtime = adapter.ManagedRichCoreAdapterV3()
    with runtime.open_slot(
        archive_root=tmp_path,
        workspace_root=config["workspace"],
        config_path=config["config"],
    ) as slot:
        view = slot.config_view()
        assert view["authority"] == "AUDIT_ONLY"
        assert view["configSha256"] == config["actualSha256"]
        assert view["configSha256"] != "f" * 64

        payload = json.loads(config["config"].read_text(encoding="utf-8"))
        payload["configSha256"] = "e" * 64
        config["config"].write_text(json.dumps(payload), encoding="utf-8")
        config["config"].chmod(0o600)
        with pytest.raises(adapter.RichCoreAdapterError) as caught:
            slot.config_view()
    assert caught.value.category == "rich_core_authority_invalid"


def test_capability_chain_is_opaque_single_use_and_mapping_forgery_has_no_authority(tmp_path):
    adapter = load_adapter_module()
    entry = binding(adapter)
    seed_initial(adapter, tmp_path, entry, message())
    slot_manager, session_manager, session = begin_session(adapter, tmp_path, entry)
    try:
        current = session.inspect_current(entry)
        for operation in (
            lambda: copy.copy(current),
            lambda: copy.deepcopy(current),
            lambda: pickle.dumps(current),
        ):
            with pytest.raises(TypeError):
                operation()
        with pytest.raises(adapter.RichCoreAdapterError) as forged:
            session.authorize_state_update({"verified": True}, requested_cursor=message()["id"])
        assert forged.value.category == "rich_core_authority_invalid"

        new = message("1540000000000000002", content="durable")
        request = adapter.IncrementalMergeRequestV3(
            schema_version=adapter.INCREMENTAL_MERGE_SCHEMA,
            entry=entry,
            observed_at="2026-09-05T04:10:00+00:00",
            messages=(new,),
            previous_cursor=message()["id"],
            new_message_ids=(new["id"],),
            partial=False,
        )
        commit = session.merge_incremental(request, pre_current=current)
        grant = session.authorize_state_update(commit, requested_cursor=new["id"])
        update = session.consume_state_update_grant(grant)
        assert update["authorizedCursor"] == new["id"]
        assert update["committedMessageIds"] == [new["id"]]
        assert update["generationId"] != "initial"

        with pytest.raises(adapter.RichCoreAdapterError) as replay:
            session.consume_state_update_grant(grant)
        assert replay.value.category == "rich_core_authority_invalid"
    finally:
        close_session(slot_manager, session_manager)


def test_mutable_only_merge_cannot_advance_cursor_and_cross_session_capability_fails(tmp_path):
    adapter = load_adapter_module()
    entry = binding(adapter)
    original = message()
    seed_initial(adapter, tmp_path, entry, original)
    slot_manager, session_manager, session = begin_session(adapter, tmp_path, entry)
    current = session.inspect_current(entry)
    updated = dict(original, pinned=True)
    request = adapter.IncrementalMergeRequestV3(
        schema_version=adapter.INCREMENTAL_MERGE_SCHEMA,
        entry=entry,
        observed_at="2026-09-05T04:20:00+00:00",
        messages=(updated,),
        previous_cursor=original["id"],
        new_message_ids=(),
        partial=False,
    )
    commit = session.merge_incremental(request, pre_current=current)
    with pytest.raises(adapter.RichCoreAdapterError) as ahead:
        session.authorize_state_update(
            commit, requested_cursor="1540000000000000002"
        )
    assert ahead.value.category == "rich_cursor_not_authorized"
    close_session(slot_manager, session_manager)

    # A capability dies with its session and cannot be replayed into a new one.
    second_slot, second_manager, second_session = begin_session(adapter, tmp_path, entry)
    try:
        with pytest.raises(adapter.RichCoreAdapterError) as stale:
            second_session.authorize_state_update(commit, requested_cursor=original["id"])
        assert stale.value.category == "rich_core_authority_invalid"
    finally:
        close_session(second_slot, second_manager)


def test_current_capability_rejects_cross_entry_substitution(tmp_path):
    adapter = load_adapter_module()
    first = binding(adapter, relative_path="test/first")
    second = binding(adapter, relative_path="test/second")
    second_body = second.audit_mapping()
    second_body["channelId"] = "1490000000000000002"
    second_body["entryBindingSha256"] = adapter.json_sha256({
        key: value
        for key, value in second_body.items()
        if key != "entryBindingSha256"
    })
    second = adapter.EntryBindingV3.from_mapping(second_body)
    seed_initial(adapter, tmp_path, first, message())
    seed_initial(
        adapter,
        tmp_path,
        second,
        dict(message("1540000000000000002"), channel_id=second.channel_id),
    )
    config = write_managed_config(adapter, tmp_path)
    runtime = adapter.ManagedRichCoreAdapterV3()
    with runtime.open_slot(
        archive_root=tmp_path,
        workspace_root=config["workspace"],
        config_path=config["config"],
    ) as slot:
        request = adapter.IncrementalBeginRequestV3(
            schema_version=adapter.INCREMENTAL_BEGIN_SCHEMA,
            role="daily-sync-1",
            archive_root=tmp_path,
            workspace_root=config["workspace"],
            state_path=config["state"],
            queue_path=config["queue"],
            openclaw_config_path=config["openclaw"],
            timezone_name="Asia/Taipei",
            inventory_digest=first.inventory_digest,
            inventory_observed_at=first.inventory_observed_at,
            entry_bindings=(first, second),
            limits=(
                ("maxEntries", 6),
                ("maxWriteEntries", 4),
                ("maxReadMessages", 180),
                ("maxMessagesPerEntry", 60),
                ("mutableRefreshLimit", 10),
            ),
        )
        with slot.begin_incremental(request) as session:
            current = session.inspect_current(first)
            update = dict(
                message("1540000000000000003"), channel_id=second.channel_id
            )
            merge = adapter.IncrementalMergeRequestV3(
                schema_version=adapter.INCREMENTAL_MERGE_SCHEMA,
                entry=second,
                observed_at="2026-09-05T04:30:00+00:00",
                messages=(update,),
                previous_cursor="1540000000000000002",
                new_message_ids=(update["id"],),
                partial=False,
            )
            with pytest.raises(adapter.RichCoreAdapterError) as caught:
                session.merge_incremental(merge, pre_current=current)
    assert caught.value.category == "rich_core_authority_invalid"


def test_capability_rejects_pid_change_and_current_drift(tmp_path, monkeypatch):
    adapter = load_adapter_module()
    entry = binding(adapter)
    original = message()
    seed_initial(adapter, tmp_path, entry, original)
    slot_manager, session_manager, session = begin_session(adapter, tmp_path, entry)
    try:
        current = session.inspect_current(entry)
        real_getpid = adapter.os.getpid
        monkeypatch.setattr(adapter.os, "getpid", lambda: real_getpid() + 1)
        with pytest.raises(adapter.RichCoreAdapterError) as pid_error:
            session.current_view(current)
        assert pid_error.value.category == "rich_core_authority_invalid"
        monkeypatch.setattr(adapter.os, "getpid", real_getpid)

        pointer = tmp_path / entry.relative_path / "CURRENT.json"
        pointer.write_text('{"generationId":"forged-generation"}\n', encoding="utf-8")
        pointer.chmod(0o600)
        update = message("1540000000000000002", content="must-not-merge")
        merge = adapter.IncrementalMergeRequestV3(
            schema_version=adapter.INCREMENTAL_MERGE_SCHEMA,
            entry=entry,
            observed_at="2026-09-05T04:40:00+00:00",
            messages=(update,),
            previous_cursor=original["id"],
            new_message_ids=(update["id"],),
            partial=False,
        )
        with pytest.raises(adapter.RichCoreAdapterError) as drift:
            session.merge_incremental(merge, pre_current=current)
        assert drift.value.category == "rich_archive_readback_failed"
    finally:
        close_session(slot_manager, session_manager)


def test_full_rebuild_interface_is_a_typed_fail_closed_stub(tmp_path):
    adapter = load_adapter_module()
    runtime = adapter.ManagedRichCoreAdapterV3()
    config = write_managed_config(adapter, tmp_path)
    with runtime.open_slot(
        archive_root=tmp_path,
        workspace_root=config["workspace"],
        config_path=config["config"],
    ) as slot:
        with pytest.raises(adapter.RichCoreAdapterError) as caught:
            slot.begin_full_rebuild(None)
    assert caught.value.category == "rich_core_contract_pending"


def test_capability_cannot_be_constructed_and_expires_inside_live_session(tmp_path):
    adapter = load_adapter_module()
    with pytest.raises(TypeError):
        adapter.CurrentReadbackCapability(guard=object())
    entry = binding(adapter)
    seed_initial(adapter, tmp_path, entry, message())
    config = write_managed_config(adapter, tmp_path)
    runtime = adapter.ManagedRichCoreAdapterV3(capability_ttl_seconds=0.001)
    with runtime.open_slot(
        archive_root=tmp_path,
        workspace_root=config["workspace"],
        config_path=config["config"],
    ) as slot:
        request = adapter.IncrementalBeginRequestV3(
            schema_version=adapter.INCREMENTAL_BEGIN_SCHEMA,
            role="daily-sync-1",
            archive_root=tmp_path,
            workspace_root=config["workspace"],
            state_path=config["state"],
            queue_path=config["queue"],
            openclaw_config_path=config["openclaw"],
            timezone_name="Asia/Taipei",
            inventory_digest=entry.inventory_digest,
            inventory_observed_at=entry.inventory_observed_at,
            entry_bindings=(entry,),
            limits=(
                ("maxEntries", 6),
                ("maxWriteEntries", 4),
                ("maxReadMessages", 180),
                ("maxMessagesPerEntry", 60),
                ("mutableRefreshLimit", 10),
            ),
        )
        with slot.begin_incremental(request) as session:
            current = session.inspect_current(entry)
            time.sleep(0.01)
            with pytest.raises(adapter.RichCoreAdapterError) as caught:
                session.current_view(current)
    assert caught.value.category == "rich_core_authority_expired"


def test_entry_binding_rejects_type_confusion_and_all_unicode_controls():
    adapter = load_adapter_module()
    valid = binding(adapter).audit_mapping()
    wrong_type = dict(valid, channelId=1490000000000000001)
    wrong_type["entryBindingSha256"] = adapter.json_sha256({
        key: value for key, value in wrong_type.items()
        if key != "entryBindingSha256"
    })
    with pytest.raises(adapter.RichCoreAdapterError):
        adapter.EntryBindingV3.from_mapping(wrong_type)

    controlled = dict(valid, relativePath="test/entry\u0085hidden")
    controlled["normalizedRelativePath"] = controlled["relativePath"].casefold()
    controlled["entryBindingSha256"] = adapter.json_sha256({
        key: value for key, value in controlled.items()
        if key != "entryBindingSha256"
    })
    with pytest.raises(adapter.RichCoreAdapterError):
        adapter.EntryBindingV3.from_mapping(controlled)


def test_non_audited_empty_baseline_cannot_mint_head_probe(tmp_path):
    adapter = load_adapter_module()
    entry = binding(adapter)
    seed_initial(adapter, tmp_path, entry, None)
    slot_manager, session_manager, session = begin_session(adapter, tmp_path, entry)
    try:
        with pytest.raises(adapter.RichCoreAdapterError) as caught:
            session.probe_head(entry, fetch_page=lambda _channel, _limit: [])
        assert caught.value.category == "rich_baseline_missing"
    finally:
        close_session(slot_manager, session_manager)
