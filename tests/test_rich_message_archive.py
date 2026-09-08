import gc
import hashlib
import importlib.util
import json
import multiprocessing
import os
import queue
import sys
import threading
import time
import types
import urllib.error
import weakref
from email.message import Message
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/rich_message_archive.py"
spec = importlib.util.spec_from_file_location("rich_message_archive", SCRIPT)
rich = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = rich
spec.loader.exec_module(rich)


OBSERVED = "2026-09-05T04:00:00Z"
LOCK_NAME = rich.CANONICAL_ARCHIVE_LOCK_NAME


def message(**overrides):
    value = {
        "id": "1540000000000000001",
        "channel_id": "1490000000000000001",
        "timestamp": "2026-09-05T03:00:00.000000+00:00",
        "edited_timestamp": None,
        "type": 0,
        "content": "hello",
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
    value.update(overrides)
    return value


def normalize(value=None, **kwargs):
    source = value or message()
    return rich.normalize_message(
        source, expected_channel_id=kwargs.pop("expected_channel_id", str(source["channel_id"])),
        observed_at=kwargs.pop("observed_at", OBSERVED), **kwargs,
    )


def make_store(tmp_path, name="test/entry"):
    return rich.RichArchiveStore(
        tmp_path / name,
        lock_path=tmp_path / LOCK_NAME,
    )


def immutable_evidence():
    return {
        "schemaVersion": "openclaw-discord-immutable-evidence-ref.v1",
        "snapshotId": "snapshot-20260905",
        "status": "PASS",
        "verifiedAt": OBSERVED,
        "archiveTreeManifestSha256": "a" * 64,
        "stateSha256": "b" * 64,
        "queueSha256": "c" * 64,
        "verificationSha256": "d" * 64,
    }


def inventory_response(entries, **overrides):
    rows = [dict(row) for row in entries]
    value = {
        "schemaVersion": rich.DISCORD_INVENTORY_RESPONSE_SCHEMA,
        "source": "discord-api-runtime-inventory",
        "request": {
            "includeActiveThreads": True,
            "includeArchivedThreads": True,
        },
        "complete": True,
        "truncated": False,
        "terminalPageObserved": True,
        "activeChannelsComplete": True,
        "activeThreadsComplete": True,
        "archivedThreadsComplete": True,
        "pageCount": 1,
        "responseCount": len(rows),
        "entries": rows,
    }
    value.update(overrides)
    return value


def page_response(messages, *, channel_id, before, limit, **overrides):
    rows = [dict(row) for row in messages]
    value = {
        "schemaVersion": rich.DISCORD_PAGE_RESPONSE_SCHEMA,
        "source": "discord-api-runtime-page",
        "request": {
            "channelId": str(channel_id),
            "before": before,
            "limit": limit,
        },
        "complete": True,
        "truncated": False,
        "responseCount": len(rows),
        "messages": rows,
    }
    value.update(overrides)
    return value


def inventory_entry(*, channel_id="1490000000000000001", relative_path="test/entry"):
    return {"channelId": str(channel_id), "relativePath": relative_path}


def archive_run_lock(archive_root):
    return rich.RichArchiveStore(
        Path(archive_root) / ".run-lock-owner",
        lock_path=rich.canonical_archive_lock_path(Path(archive_root)),
    ).acquire_lock()


def context_lock_token(run_context):
    return rich._require_run_context(run_context)["lockToken"]


def _cross_process_merge_worker(
    archive_root_text,
    lock_name,
    label,
    message_id,
    generation_id,
    ready,
    release,
    result_queue,
):
    """Independent-process lost-update regression worker."""
    archive_root = Path(archive_root_text)
    entry_root = archive_root / "test/entry"
    store = rich.RichArchiveStore(entry_root, lock_path=archive_root / lock_name)
    lock_token = None
    run_context = None
    try:
        lock_token = store.acquire_lock()
        run_context = rich.begin_incremental_run(
            entries=[inventory_entry()],
            archive_root=archive_root,
            lock_token=lock_token,
        )
        original = rich.RichArchiveStore._require_stage_base_current_unchanged

        def gated_compare(self, stage):
            original(self, stage)
            ready.set()
            if not release.wait(timeout=10):
                raise RuntimeError("multiprocess publish gate timed out")

        rich.RichArchiveStore._require_stage_base_current_unchanged = gated_compare
        outcome = store.merge_messages(
            [message(id=message_id, content=f"{label} concurrent update")],
            channel_id="1490000000000000001",
            relative_path="test/entry",
            observed_at=OBSERVED,
            generation_id=generation_id,
            run_context=run_context,
        )
        result_queue.put((label, "ok", message_id, outcome.get("verified")))
    except BaseException as exc:
        result_queue.put((label, "error", type(exc).__name__, str(exc)))
    finally:
        if run_context is not None:
            run_context.close()
        elif lock_token is not None:
            lock_token.close()


def _cross_process_publish_worker(
    archive_root_text,
    lock_name,
    label,
    stage_text,
    generation_id,
    generation_sha256,
    ready,
    release,
    result_queue,
    use_run_context,
):
    """Race public publish through canonical versus caller-selected lock paths."""
    archive_root = Path(archive_root_text)
    store = rich.RichArchiveStore(
        archive_root / "test/entry",
        lock_path=archive_root / lock_name,
    )
    lock_token = None
    run_context = None
    try:
        lock_token = store.acquire_lock()
        if use_run_context:
            run_context = rich.begin_incremental_run(
                entries=[inventory_entry()],
                archive_root=archive_root,
                lock_token=lock_token,
            )
            original = rich.RichArchiveStore._require_stage_base_current_unchanged

            def gated_compare(self, stage):
                original(self, stage)
                ready.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("multiprocess public publish gate timed out")

            rich.RichArchiveStore._require_stage_base_current_unchanged = gated_compare
        store.publish_stage(
            Path(stage_text),
            generation_id,
            generation_sha256,
            lock_token=lock_token,
            run_context=run_context,
        )
        result_queue.put((label, "ok"))
    except BaseException as exc:
        result_queue.put((label, "error", type(exc).__name__, str(exc)))
    finally:
        if run_context is not None:
            run_context.close()
        elif lock_token is not None:
            lock_token.close()


def full_run_context(archive_root, entries=None, *, limits=None):
    rows = list(entries if entries is not None else [inventory_entry()])
    lock_token = archive_run_lock(archive_root)
    try:
        return rich.begin_full_rebuild_run(
            fetch_inventory=lambda: inventory_response(rows),
            expected_entries=rows,
            archive_root=archive_root,
            lock_token=lock_token,
            limits=limits,
        )
    except BaseException:
        lock_token.close()
        raise


def incremental_run_context(archive_root, entries=None, *, limits=None):
    rows = list(entries if entries is not None else [inventory_entry()])
    lock_token = archive_run_lock(archive_root)
    try:
        return rich.begin_incremental_run(
            entries=rows,
            archive_root=archive_root,
            lock_token=lock_token,
            limits=limits,
        )
    except BaseException:
        lock_token.close()
        raise


def live_evidence(
    store,
    generation_id,
    run_context,
    messages=None,
    *,
    channel_id="1490000000000000001",
    relative_path="test/entry",
    evidence_ttl_seconds=rich.DEFAULT_LIVE_EVIDENCE_TTL_SECONDS,
):
    rows = sorted(
        list(messages if messages is not None else [message()]),
        key=lambda row: int(row["id"]),
        reverse=True,
    )
    calls = {"count": 0}

    expected_channel_id = str(channel_id)

    def fetch_page(requested_channel_id, *, before, limit):
        assert requested_channel_id == expected_channel_id
        calls["count"] += 1
        if calls["count"] == 1:
            assert before is None and limit == 1
            selected = rows[:1]
        else:
            selected = [
                row for row in rows
                if before is None or int(row["id"]) < int(before)
            ][:limit]
        return page_response(
            selected,
            channel_id=requested_channel_id,
            before=before,
            limit=limit,
        )

    return rich.collect_live_evidence(
        fetch_page=fetch_page,
        verify_immutable_evidence=immutable_evidence,
        run_context=run_context,
        entry_root=store.entry_root,
        generation_id=generation_id,
        channel_id=str(channel_id),
        relative_path=relative_path,
        evidence_ttl_seconds=evidence_ttl_seconds,
    )


def test_live_collector_records_exact_backward_page_chain_and_terminal_page(tmp_path):
    store = make_store(tmp_path)
    rows = [
        message(id="1540000000000000003", content="third"),
        message(id="1540000000000000002", content="second"),
        message(id="1540000000000000001", content="first"),
    ]
    calls = []

    def fetch_page(channel_id, *, before, limit):
        calls.append((channel_id, before, limit))
        if before is None:
            selected = rows[:1]
        else:
            selected = [row for row in rows if int(row["id"]) < int(before)][:limit]
        return page_response(
            selected,
            channel_id=channel_id,
            before=before,
            limit=limit,
        )

    with full_run_context(tmp_path) as run_context:
        with rich.collect_live_evidence(
            fetch_page=fetch_page,
            verify_immutable_evidence=immutable_evidence,
            run_context=run_context,
            entry_root=store.entry_root,
            generation_id="collector",
            channel_id="1490000000000000001",
            relative_path="test/entry",
            page_limit=2,
        ) as token:
            evidence = token.audit_evidence()

    assert calls == [
        ("1490000000000000001", None, 1),
        ("1490000000000000001", "1540000000000000004", 2),
        ("1490000000000000001", "1540000000000000002", 2),
        ("1490000000000000001", "1540000000000000001", 2),
    ]
    pages = evidence["enumeration"]["pages"]
    assert [page["requestBefore"] for page in pages] == [
        "1540000000000000004", "1540000000000000002", "1540000000000000001",
    ]
    assert [page["terminal"] for page in pages] == [False, False, True]
    assert [page["responseEnvelope"]["responseCount"] for page in pages] == [2, 1, 0]
    assert [row["messageId"] for row in evidence["messages"]] == [
        "1540000000000000001",
        "1540000000000000002",
        "1540000000000000003",
    ]


def test_live_collector_rejects_repeated_pages_and_unproven_terminal_page(tmp_path):
    store = make_store(tmp_path)
    row = message()
    calls = {"count": 0}

    def repeated_page(channel_id, *, before, limit):
        calls["count"] += 1
        if before is None:
            selected = [row]
        else:
            selected = [row]
        return page_response(
            selected,
            channel_id=channel_id,
            before=before,
            limit=limit,
        )

    common = {
        "verify_immutable_evidence": immutable_evidence,
        "entry_root": store.entry_root,
        "generation_id": "collector",
        "channel_id": "1490000000000000001",
        "relative_path": "test/entry",
    }
    with full_run_context(tmp_path) as run_context:
        with pytest.raises(rich.GenerationError, match="repeated a message ID"):
            rich.collect_live_evidence(
                fetch_page=repeated_page,
                run_context=run_context,
                page_limit=1,
                max_pages=3,
                **common,
            )

    calls["count"] = 0

    def never_terminal(channel_id, *, before, limit):
        calls["count"] += 1
        selected = [message(id="1540000000000000002")]
        return page_response(
            selected,
            channel_id=channel_id,
            before=before,
            limit=limit,
        )

    with full_run_context(tmp_path) as run_context:
        with pytest.raises(rich.GenerationError, match="page bound reached"):
            rich.collect_live_evidence(
                fetch_page=never_terminal,
                run_context=run_context,
                page_limit=1,
                max_pages=1,
                **common,
            )


def test_live_collector_rejects_duplicate_inventory_identity(tmp_path):
    with archive_run_lock(tmp_path) as lock_token:
        with pytest.raises(rich.GenerationError, match="duplicate channel"):
            rich.begin_full_rebuild_run(
                fetch_inventory=lambda: inventory_response([
                    {"channelId": "1490000000000000001", "relativePath": "test/entry"},
                    {"channelId": "1490000000000000001", "relativePath": "test/other"},
                ]),
                expected_entries=[inventory_entry()],
                archive_root=tmp_path,
                lock_token=lock_token,
            )


def test_live_pass_authority_cannot_be_self_attested_forged_or_reused_after_close(tmp_path):
    assert not hasattr(rich, "build_live_inventory_evidence")
    with pytest.raises(rich.GenerationError, match="runtime live evidence token"):
        rich.verify_full_generation(tmp_path, live_evidence_token={})

    forged = rich.LiveEvidenceToken(guard=rich._LIVE_EVIDENCE_GUARD)
    try:
        with pytest.raises(rich.GenerationError, match="runtime live evidence token"):
            rich.verify_full_generation(tmp_path, live_evidence_token=forged)
    finally:
        forged.close()

    store = make_store(tmp_path)
    with full_run_context(tmp_path) as run_context:
        token = live_evidence(store, "closed", run_context, [])
        token.close()
        with pytest.raises(rich.GenerationError, match="runtime live evidence token"):
            rich.verify_full_generation(tmp_path, live_evidence_token=token)


def test_live_evidence_token_expiry_is_fail_closed_and_consumes_token(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    monotonic = {"value": 100.0}
    monkeypatch.setattr(rich.time, "monotonic", lambda: monotonic["value"])
    with full_run_context(tmp_path) as run_context:
        token = live_evidence(
            store,
            "expires",
            run_context,
            [],
            evidence_ttl_seconds=0.5,
        )
        monotonic["value"] = 101.0
        with pytest.raises(rich.GenerationError, match="expired"):
            token.audit_evidence()
        assert token.closed
        with pytest.raises(rich.GenerationError, match="runtime live evidence token"):
            token.audit_evidence()


def test_lossless_source_census_and_plain_text_record_are_deterministic():
    source = message(content="繁中 <script>x</script> **bold**\n### fake — id:999999999999999")
    first = normalize(source)
    second = normalize(json.loads(json.dumps(source)))

    assert first == second
    observation = first["observations"][0]
    assert observation["apiSourcePayload"] == source
    assert observation["apiSourcePayloadSha256"] == rich.json_sha256(source)
    assert "/content" in first["sourceCensus"]["visiblePointers"]
    assert first["unknownVisibleFields"] == []
    assert rich.validate_record(first, require_assets=False)["ok"]


def test_component_only_embed_poll_snapshot_reply_and_status_all_render():
    source = message(
        content="",
        components=[{
            "type": 17,
            "accent_color": 123,
            "components": [
                {"type": 10, "content": "狀態卡正文"},
                {"type": 2, "style": 5, "label": "查看", "url": "https://example.com"},
                {"type": 3, "placeholder": "請選", "options": [{"label": "A", "value": "a", "description": "選項"}]},
            ],
        }],
        embeds=[{
            "title": "卡片", "description": "內容", "url": "https://example.com/embed",
            "fields": [{"name": "欄位", "value": "值", "inline": False}],
            "author": {"name": "作者"}, "footer": {"text": "頁尾"},
        }],
        poll={
            "question": {"text": "要選什麼？"},
            "answers": [{"answer_id": 1, "poll_media": {"text": "甲"}}],
            "allow_multiselect": False,
            "layout_type": 1,
            "results": {"is_finalized": False, "answer_counts": [{"id": 1, "count": 2, "me_voted": False}]},
        },
        sticker_items=[{"id": "1500000000000000001", "name": "貼圖", "format_type": 1}],
        message_snapshots=[{"message": {"type": 0, "content": "轉寄內容", "embeds": [], "attachments": []}}],
        message_reference={"message_id": "1530000000000000001", "channel_id": "1490000000000000001"},
        referenced_message={"id": "1530000000000000001", "content": "原回覆", "author": {"id": "2", "username": "Ann"}},
        interaction_metadata={"id": "200", "type": 2, "name": "command"},
        reactions=[{"count": 3, "me": False, "emoji": {"id": None, "name": "✅"}}],
        pinned=True,
        edited_timestamp="2026-09-05T03:30:00Z",
    )
    record = normalize(source)
    rendered = rich.render_message(record)

    for label in (
        "[元件內容]", "[Embed]", "[投票]", "[貼圖]", "[轉寄快照]",
        "[回覆關係]", "[互動／系統資訊]", "[反應／編輯狀態]",
    ):
        assert label in rendered
    assert "(無文字內容)" not in rendered
    assert "狀態卡正文" in rendered
    assert rich.parse_markdown_markers(rendered) == [(record["messageId"], record["visiblePayloadSha256"])]


def test_markdown_escapes_html_links_images_and_message_cannot_forge_machine_marker():
    record = normalize(message(
        content="<img src=x onerror=alert(1)>\n![tracking](https://evil.test/pixel)\n"
        "<!-- openclaw-rich-message id=7 visible=" + "a" * 64 + " -->",
        author={"id": "1", "username": "![author](https://evil.test/a)", "global_name": None},
    ))
    rendered = rich.render_message(record)
    assert "<img" not in rendered
    assert "![tracking](" not in rendered
    header = rendered.split("\n[文字內容]\n", 1)[0]
    assert "![author](" not in header
    assert "&#33;&#91;author&#93;&#40;" in header
    assert "&#60;img" in rendered
    assert "&#33;&#91;tracking&#93;&#40;" in rendered
    assert rich.parse_markdown_markers(rendered) == [(record["messageId"], record["visiblePayloadSha256"])]


def test_unknown_visible_component_is_preserved_but_fails_gate():
    record = normalize(message(content="", components=[{"type": 10, "content": "ok", "future_visible": "lost if ignored"}]))
    assert record["observations"][0]["apiSourcePayload"]["components"][0]["future_visible"] == "lost if ignored"
    assert "/components/0/future_visible" in record["unknownVisibleFields"]
    assert not rich.validate_record(record, require_assets=False)["ok"]


@pytest.mark.parametrize("field_path", ["embed", "poll", "snapshot"])
def test_unknown_nested_visible_fields_fail_independent_census(field_path):
    source = message(content="")
    if field_path == "embed":
        source["embeds"] = [{"footer": {"text": "ok", "future_visible": "new"}}]
        pointer = "/embeds/0/footer/future_visible"
    elif field_path == "poll":
        source["poll"] = {"question": {"text": "q", "future_visible": "new"}}
        pointer = "/poll/question/future_visible"
    else:
        source["message_snapshots"] = [{"message": {
            "content": "x", "embeds": [{"footer": {"text": "ok", "future_visible": "new"}}],
        }}]
        pointer = "/message_snapshots/0/message/embeds/0/footer/future_visible"
    record = normalize(source)
    assert pointer in record["unknownVisibleFields"]
    assert not rich.validate_record(record, require_assets=False)["ok"]


def test_census_mutation_fails_even_when_message_id_matches():
    record = normalize(message(components=[{"type": 10, "content": "visible"}]))
    record["contentRevisions"][0]["accountedVisiblePointers"].remove("/components/0/content")
    with pytest.raises(rich.SourceCensusError):
        rich.validate_record(record, require_assets=False)


def test_content_revisions_and_mutable_observations_are_retained():
    first = normalize(message(content="v1", reactions=[]), observed_at="2026-09-05T04:00:00Z")
    reacted = normalize(message(content="v1", reactions=[{"count": 1, "me": False, "emoji": {"name": "✅"}}]), observed_at="2026-09-05T04:10:00Z")
    edited = normalize(message(content="v2", edited_timestamp="2026-09-05T04:20:00Z"), observed_at="2026-09-05T04:21:00Z")

    merged = rich.merge_message_records(rich.merge_message_records(first, reacted), edited)
    assert len(merged["contentRevisions"]) == 2
    assert len(merged["observations"]) == 3
    revision, observation = rich._active_parts(merged)
    assert revision["content"]["content"] == "v2"
    assert observation["observedAt"] == "2026-09-05T04:21:00Z"
    assert "v1" in rich.render_message(merged)
    assert "[歷史內容版本]" in rich.render_message(merged)


def test_historical_revision_and_observation_corruption_cannot_hide_behind_active():
    first = normalize(message(content="v1"), observed_at="2026-09-05T04:00:00Z")
    second = normalize(
        message(content="v2", edited_timestamp="2026-09-05T04:10:00Z"),
        observed_at="2026-09-05T04:11:00Z",
    )
    merged = rich.merge_message_records(first, second)
    merged["observations"][0]["apiSourcePayload"]["content"] = "corrupt"
    with pytest.raises(rich.RichArchiveError, match="hash mismatch"):
        rich.validate_record(merged, require_assets=False)


def test_signed_cdn_query_does_not_create_content_revision_but_is_observed():
    a = message(attachments=[{
        "id": "900", "filename": "x.png", "size": 3,
        "url": "https://cdn.discordapp.com/attachments/1/x.png?ex=1&is=2&hm=aaa",
    }])
    b = json.loads(json.dumps(a))
    b["attachments"][0]["url"] = "https://cdn.discordapp.com/attachments/1/x.png?ex=9&is=8&hm=bbb"
    first = normalize(a, observed_at="2026-09-05T04:00:00Z")
    second = normalize(b, observed_at="2026-09-05T04:10:00Z")
    merged = rich.merge_message_records(first, second)
    assert len(merged["contentRevisions"]) == 1
    assert len(merged["observations"]) == 2


def test_resume_live_binding_ignores_only_verified_cdn_signature_churn():
    first_source = message(attachments=[{
        "id": "900", "filename": "x.png", "size": 3,
        "url": "https://cdn.discordapp.com/attachments/1/x.png?ex=1&is=2&hm=aaa",
    }])
    second_source = json.loads(json.dumps(first_source))
    second_source["attachments"][0]["url"] = (
        "https://cdn.discordapp.com/attachments/1/x.png?ex=9&is=8&hm=bbb"
    )
    first = rich._active_live_binding(normalize(first_source))
    second = rich._active_live_binding(normalize(second_source))
    assert first != second
    assert rich._resume_stable_live_binding(first) == rich._resume_stable_live_binding(second)

    changed = json.loads(json.dumps(second))
    changed["assets"][0]["displayFilename"] = "different.png"
    assert rich._resume_stable_live_binding(first) != rich._resume_stable_live_binding(changed)


def test_attachment_description_changes_searchable_markdown_and_visible_fingerprint():
    first_source = message(attachments=[{
        "id": "900", "filename": "x.png", "description": "第一版說明", "size": 3,
        "height": 10, "width": 20,
        "url": "https://cdn.discordapp.com/attachments/1/x.png",
    }])
    second_source = json.loads(json.dumps(first_source))
    second_source["attachments"][0]["description"] = "第二版說明"
    first = normalize(first_source)
    second = normalize(second_source)
    assert first["visiblePayloadSha256"] != second["visiblePayloadSha256"]
    assert "第一版說明" in rich.render_message(first)
    assert "第二版說明" in rich.render_message(second)


def test_recursive_asset_inventory_uses_pointer_identity_and_marks_external_metadata_only():
    record = normalize(message(
        attachments=[{
            "id": "900", "filename": "../同名.PNG", "size": 3,
            "url": "https://cdn.discordapp.com/attachments/1/a?ex=1",
        }],
        embeds=[{
            "image": {"url": "https://media.discordapp.net/attachments/1/image.png", "width": 2, "height": 2},
            "thumbnail": {"url": "https://example.com/external.png"},
        }],
        components=[{"type": 12, "items": [{"media": {"url": "https://cdn.discordapp.com/attachments/1/component.png"}}]}],
        message_snapshots=[{"message": {"content": "x", "attachments": [{
            "id": "901", "filename": "same.png", "size": 4,
            "url": "https://cdn.discordapp.com/attachments/1/snapshot.png",
        }]}}],
    ))
    assets = record["observations"][0]["assetInventory"]
    assert len(assets) >= 5
    assert len({asset["assetId"] for asset in assets}) == len(assets)
    assert all(".." not in (asset["displayFilename"] or "") for asset in assets)
    external = next(asset for asset in assets if asset["remoteUrl"].startswith("https://example.com"))
    assert not external["inScope"]
    assert external["status"] == "metadata_only"


def test_capacity_preflight_requires_declared_sizes_and_reserve(monkeypatch, tmp_path):
    asset = normalize(message(attachments=[{
        "id": "900", "filename": "x", "size": 3,
        "url": "https://cdn.discordapp.com/attachments/1/x",
    }]))["observations"][0]["assetInventory"][0]
    monkeypatch.setattr(rich.shutil, "disk_usage", lambda _path: types.SimpleNamespace(free=100))
    with pytest.raises(rich.AssetDownloadError, match="insufficient disk"):
        rich.preflight_asset_capacity([asset], tmp_path, limits=rich.AssetLimits(disk_reserve_bytes=101))

    missing = dict(asset, declaredSize=None)
    with pytest.raises(rich.AssetDownloadError, match="missing declared size"):
        rich.preflight_asset_capacity([missing], tmp_path, limits=rich.AssetLimits(disk_reserve_bytes=0))


class FakeResponse:
    def __init__(self, body=b"abc", status=200, headers=None):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = headers or {"Content-Length": str(len(body)), "Content-Encoding": "identity"}
        self.closed = False

    def read(self, size=-1):
        if size < 0:
            size = len(self._body)
        data = self._body[self._offset:self._offset + size]
        self._offset += len(data)
        return data

    def close(self):
        self.closed = True

    def getcode(self):
        return self.status


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=0):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def global_resolver(host, port, type=0):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def pending_asset(url="https://cdn.discordapp.com/attachments/1/x", size=3):
    return normalize(message(attachments=[{
        "id": "900", "filename": "x.bin", "size": size, "url": url,
    }]))["observations"][0]["assetInventory"][0]


def test_downloader_has_no_bot_auth_cookie_or_proxy_headers_and_hashes_file(tmp_path):
    opener = FakeOpener([FakeResponse()])
    downloader = rich.AssetDownloader(opener=opener, resolver=global_resolver, limits=rich.AssetLimits(disk_reserve_bytes=0))
    result = downloader.download(pending_asset(), tmp_path)
    target = rich.contained_path(tmp_path, result["localRelativePath"])

    assert target.read_bytes() == b"abc"
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert result["sha256"] == hashlib.sha256(b"abc").hexdigest()
    sent = {key.lower(): value for key, value in opener.requests[0].header_items()}
    assert "authorization" not in sent
    assert "cookie" not in sent
    assert sent["accept-encoding"] == "identity"


def attachment_with_proxy():
    return message(attachments=[{
        "id": "900",
        "filename": "historical.bin",
        "size": 3,
        "content_type": "application/octet-stream",
        "url": "https://cdn.discordapp.com/attachments/1/historical.bin",
        "proxy_url": "https://media.discordapp.net/attachments/1/historical.bin",
    }])


def test_expired_direct_attachment_recovers_from_verified_equivalent_proxy(tmp_path):
    proxy_response = FakeResponse(body=b"abc")
    direct_response = FakeResponse(body=b"missing", status=404)
    downloader = rich.AssetDownloader(
        opener=FakeOpener([proxy_response, direct_response]),
        resolver=global_resolver,
        limits=rich.AssetLimits(disk_reserve_bytes=0),
    )

    recovered = rich.apply_asset_results(
        normalize(attachment_with_proxy()), downloader, tmp_path,
    )
    assets = recovered["observations"][0]["assetInventory"]
    proxy = next(row for row in assets if row["kind"] == "attachment_proxy")
    direct = next(row for row in assets if row["kind"] == "attachment")

    assert recovered["attachmentErrors"] == []
    assert proxy["status"] == direct["status"] == "complete"
    assert proxy["sha256"] == direct["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert proxy["byteLength"] == direct["byteLength"] == 3
    assert direct["recoveryMethod"] == "equivalent_discord_attachment_variant"
    assert direct["recoveredFromAssetId"] == proxy["assetId"]
    assert "HTTP status 404" in direct["recoveryOriginalError"]
    proxy_path = rich.contained_path(tmp_path, proxy["localRelativePath"])
    direct_path = rich.contained_path(tmp_path, direct["localRelativePath"])
    assert proxy_path != direct_path
    assert proxy_path.read_bytes() == direct_path.read_bytes() == b"abc"
    assert rich.validate_record(
        recovered, require_assets=True, generation_root=tmp_path,
    )["ok"]


@pytest.mark.parametrize("field,value", [
    ("recoveryMethod", "unverified_copy"),
    ("recoveredFromAssetId", "not-a-sibling"),
    ("recoveryOriginalError", ""),
])
def test_equivalent_attachment_recovery_provenance_is_fail_closed(tmp_path, field, value):
    recovered = rich.apply_asset_results(
        normalize(attachment_with_proxy()),
        rich.AssetDownloader(
            opener=FakeOpener([FakeResponse(body=b"abc"), FakeResponse(status=404)]),
            resolver=global_resolver,
            limits=rich.AssetLimits(disk_reserve_bytes=0),
        ),
        tmp_path,
    )
    direct = next(
        row for row in recovered["observations"][0]["assetInventory"]
        if row["kind"] == "attachment"
    )
    direct[field] = value
    with pytest.raises(rich.RichArchiveError, match="asset recovery"):
        rich.validate_record(recovered, require_assets=True, generation_root=tmp_path)


def test_non_equivalent_attachment_variants_are_not_recovered(tmp_path):
    record = normalize(attachment_with_proxy())
    proxy, direct = record["observations"][0]["assetInventory"]
    source_path = rich.contained_path(tmp_path, proxy["localRelativePath"])
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"abc")
    proxy.update({
        "status": "complete",
        "byteLength": 3,
        "sha256": hashlib.sha256(b"abc").hexdigest(),
    })
    direct.update({"status": "error", "error": "HTTP status 404"})
    direct["displayFilename"] = "different.bin"

    output = rich._recover_equivalent_attachment_variants([proxy, direct], tmp_path)

    assert next(row for row in output if row["kind"] == "attachment")["status"] == "error"


def test_disagreeing_equivalent_completed_variants_fail_closed(tmp_path):
    record = normalize(attachment_with_proxy())
    proxy, direct = record["observations"][0]["assetInventory"]
    first_path = rich.contained_path(tmp_path, proxy["localRelativePath"])
    second_path = rich.contained_path(tmp_path, direct["localRelativePath"])
    first_path.parent.mkdir(parents=True)
    first_path.write_bytes(b"abc")
    second_path.write_bytes(b"xyz")
    proxy.update({
        "status": "complete", "byteLength": 3,
        "sha256": hashlib.sha256(b"abc").hexdigest(),
    })
    direct.update({
        "status": "complete", "byteLength": 3,
        "sha256": hashlib.sha256(b"xyz").hexdigest(),
    })
    failed = dict(direct)
    failed.update({
        "assetId": "failed-copy", "status": "error", "error": "HTTP status 404",
        "localRelativePath": "attachments/1540000000000000001/failed-copy.bin",
    })

    with pytest.raises(rich.AssetDownloadError, match="variants disagree"):
        rich._recover_equivalent_attachment_variants([proxy, direct, failed], tmp_path)


def test_both_current_discord_variants_can_record_verified_size_drift(tmp_path):
    downloader = rich.AssetDownloader(
        opener=FakeOpener([
            FakeResponse(body=b"ab"),
            FakeResponse(body=b"wxyz"),
            FakeResponse(body=b"ab"),
            FakeResponse(body=b"wxyz"),
        ]),
        resolver=global_resolver,
        limits=rich.AssetLimits(disk_reserve_bytes=0),
    )

    recovered = rich.apply_asset_results(
        normalize(attachment_with_proxy()), downloader, tmp_path,
    )
    assets = recovered["observations"][0]["assetInventory"]

    assert recovered["attachmentErrors"] == []
    assert {row["kind"]: row["declaredSize"] for row in assets} == {
        "attachment_proxy": 2,
        "attachment": 4,
    }
    assert all(row["sourceDeclaredSize"] == 3 for row in assets)
    assert all(row["sizeSource"] == "http_get_content_length" for row in assets)
    assert all(row["sourceSizeMismatch"] is True for row in assets)
    assert all(row["status"] == "complete" for row in assets)
    assert all("recoveryMethod" not in row for row in assets)
    assert rich.validate_record(
        recovered, require_assets=True, generation_root=tmp_path,
    )["ok"]


def test_size_drift_requires_both_exact_attachment_variants(tmp_path):
    downloader = rich.AssetDownloader(
        opener=FakeOpener([
            FakeResponse(body=b"ab"),
            FakeResponse(status=404),
        ]),
        resolver=global_resolver,
        limits=rich.AssetLimits(disk_reserve_bytes=0),
    )

    result = rich.apply_asset_results(
        normalize(attachment_with_proxy()), downloader, tmp_path,
    )

    assert len(result["attachmentErrors"]) == 2
    assert all(
        row["status"] == "error"
        for row in result["observations"][0]["assetInventory"]
    )


def test_size_drift_receipt_tampering_fails_closed(tmp_path):
    record = rich.apply_asset_results(
        normalize(attachment_with_proxy()),
        rich.AssetDownloader(
            opener=FakeOpener([
                FakeResponse(body=b"ab"), FakeResponse(body=b"wxyz"),
                FakeResponse(body=b"ab"), FakeResponse(body=b"wxyz"),
            ]),
            resolver=global_resolver,
            limits=rich.AssetLimits(disk_reserve_bytes=0),
        ),
        tmp_path,
    )
    record["observations"][0]["assetInventory"][0]["sourceSizeMismatch"] = False

    with pytest.raises(rich.RichArchiveError, match="Discord-declared asset size"):
        rich.validate_record(record, require_assets=True, generation_root=tmp_path)


def test_missing_sticker_size_is_resolved_by_safe_head_without_credentials():
    source = message(content="", sticker_items=[{
        "id": "1500000000000000001", "name": "貼圖", "format_type": 1,
    }])
    record = normalize(source)
    assert record["observations"][0]["assetInventory"][0]["declaredSize"] is None
    opener = FakeOpener([FakeResponse(body=b"abc")])
    downloader = rich.AssetDownloader(opener=opener, resolver=global_resolver)
    resolved = rich.resolve_asset_sizes(record, downloader)
    asset = resolved["observations"][0]["assetInventory"][0]
    assert asset["declaredSize"] == 3
    assert asset["sourceDeclaredSize"] is None
    assert asset["sizeSource"] == "http_head"
    request = opener.requests[0]
    assert request.get_method() == "HEAD"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert "authorization" not in headers and "cookie" not in headers
    assert rich.validate_record(resolved, require_assets=False)["ok"]


def test_unknown_size_asset_quota_fails_before_any_head_request(tmp_path):
    store = make_store(tmp_path)
    write_initial_generation(store, tmp_path, normalize())
    source = message(id="1540000000000000002", content="", components=[{
        "type": 12,
        "items": [
            {"media": {"url": "https://cdn.discordapp.com/attachments/1/a.png"}},
            {"media": {"url": "https://cdn.discordapp.com/attachments/1/b.png"}},
        ],
    }])
    opener = FakeOpener([])
    downloader = rich.AssetDownloader(
        opener=opener,
        resolver=global_resolver,
        limits=rich.AssetLimits(per_entry_files=1, disk_reserve_bytes=0),
    )
    with incremental_run_context(tmp_path, limits=downloader.limits) as run_context:
        with pytest.raises(rich.AssetDownloadError, match="file quota"):
            store.merge_messages(
                [source], channel_id=source["channel_id"], relative_path="test/entry",
                observed_at=OBSERVED, generation_id="over-quota", downloader=downloader,
                run_context=run_context,
            )
    assert opener.requests == []
    assert not (store.staging / "over-quota").exists()


def test_unknown_size_probe_budget_is_shared_across_batch_records(tmp_path):
    store = make_store(tmp_path)
    write_initial_generation(store, tmp_path, normalize())
    first = message(
        id="1540000000000000002", content="",
        components=[{"type": 12, "items": [{"media": {"url": "https://cdn.discordapp.com/attachments/1/a.png"}}]}],
    )
    second = message(
        id="1540000000000000003", content="",
        components=[{"type": 12, "items": [{"media": {"url": "https://cdn.discordapp.com/attachments/1/b.png"}}]}],
    )
    opener = FakeOpener([])
    downloader = rich.AssetDownloader(
        opener=opener,
        resolver=global_resolver,
        limits=rich.AssetLimits(max_unknown_size_probes=1, disk_reserve_bytes=0),
    )
    with incremental_run_context(tmp_path, limits=downloader.limits) as run_context:
        with pytest.raises(rich.AssetDownloadError, match="probe quota"):
            store.merge_messages(
                [first, second], channel_id=first["channel_id"], relative_path="test/entry",
                observed_at=OBSERVED, generation_id="shared-budget", downloader=downloader,
                run_context=run_context,
            )
    assert opener.requests == []


def test_downloader_closes_response_on_non_success_and_validation_error(tmp_path):
    non_success = FakeResponse(status=500)
    downloader = rich.AssetDownloader(opener=FakeOpener([non_success]), resolver=global_resolver)
    with pytest.raises(rich.AssetDownloadError, match="HTTP status 500"):
        downloader.download(pending_asset(), tmp_path)
    assert non_success.closed

    malformed = FakeResponse(headers={"Content-Length": "nope", "Content-Encoding": "identity"})
    downloader = rich.AssetDownloader(opener=FakeOpener([malformed]), resolver=global_resolver)
    with pytest.raises(rich.AssetDownloadError, match="Content-Length"):
        downloader.download(pending_asset(), tmp_path)
    assert malformed.closed


def test_invalid_asset_port_is_metadata_only_without_parser_exception():
    record = normalize(message(attachments=[{
        "id": "900", "filename": "x", "size": 1,
        "url": "https://cdn.discordapp.com:notaport/attachments/1/x",
    }]))
    asset = record["observations"][0]["assetInventory"][0]
    assert asset["inScope"] is False
    assert asset["scopeReason"] == "invalid_url"


@pytest.mark.parametrize("url", [
    "http://cdn.discordapp.com/attachments/1/x",
    "https://user:pw@cdn.discordapp.com/attachments/1/x",
    "https://127.0.0.1/attachments/1/x",
    "https://cdn.discordapp.com:444/attachments/1/x",
    "https://cdn.discordapp.com.evil.test/attachments/1/x",
])
def test_downloader_rejects_ssrf_shaped_urls(url, tmp_path):
    downloader = rich.AssetDownloader(opener=FakeOpener([]), resolver=global_resolver)
    asset = pending_asset(url=url)
    asset.update({
        "inScope": True,
        "localRelativePath": f"attachments/1540000000000000001/{hashlib.sha256(url.encode()).hexdigest()}.bin",
    })
    with pytest.raises(rich.AssetDownloadError):
        downloader.download(asset, tmp_path)


def test_downloader_rejects_private_dns_and_cross_host_redirect(tmp_path):
    private = lambda host, port, type=0: [(2, 1, 6, "", ("169.254.169.254", port))]
    with pytest.raises(rich.AssetDownloadError, match="non-global"):
        rich.AssetDownloader(opener=FakeOpener([]), resolver=private).download(pending_asset(), tmp_path)

    headers = Message()
    headers["Location"] = "https://media.discordapp.net/attachments/1/x"
    redirect = urllib.error.HTTPError("x", 302, "redirect", headers, None)
    with pytest.raises(rich.AssetDownloadError, match="cross-host"):
        rich.AssetDownloader(opener=FakeOpener([redirect]), resolver=global_resolver).download(pending_asset(), tmp_path)


def test_downloader_rejects_oversize_and_truncated_stream(tmp_path):
    with pytest.raises(rich.AssetDownloadError, match="per-file"):
        rich.AssetDownloader(
            opener=FakeOpener([]), resolver=global_resolver,
            limits=rich.AssetLimits(per_file_bytes=2),
        ).download(pending_asset(size=3), tmp_path)

    with pytest.raises(rich.AssetDownloadError, match="Content-Length"):
        rich.AssetDownloader(
            opener=FakeOpener([FakeResponse(body=b"ab")]), resolver=global_resolver,
        ).download(pending_asset(size=3), tmp_path)


def write_initial_generation(store, tmp_path, record):
    relative_path = store.entry_root.relative_to(Path(tmp_path).absolute()).as_posix()
    run_context = incremental_run_context(tmp_path, entries=[inventory_entry(
        channel_id=record["channelId"],
        relative_path=relative_path,
    )])
    try:
        lock_token = context_lock_token(run_context)
        stage = store.create_stage(
            "initial",
            copy_current=False,
            lock_token=lock_token,
            run_context=run_context,
        )
        rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [record])
        rich._atomic_bytes(stage / "raw/2026-09-05.md", rich.render_day([record]).encode())
        rich.atomic_json(stage / "receipts/rich-archive-latest.json", {
            "schemaVersion": rich.ENTRY_RECEIPT_SCHEMA,
            "gateStatus": "INCOMPLETE",
            "reason": "test",
        })
        manifest = rich.generation_inventory(stage)
        rich.atomic_json(stage / "generation-manifest.json", manifest)
        store.publish_stage(
            stage,
            "initial",
            manifest["generationSha256"],
            lock_token=lock_token,
            run_context=run_context,
        )
    finally:
        run_context.close()


def test_generation_pointer_is_atomic_checksummed_and_never_claims_full_pass(tmp_path):
    store = make_store(tmp_path)
    record = normalize()
    write_initial_generation(store, tmp_path, record)

    current = store.resolve_current()
    assert current is not None and current.name == "initial"
    result = rich.verify_generation(current)
    assert result["ok"]
    assert result["gateStatus"] == "INCOMPLETE"
    assert not result["fullGatePresent"]
    with pytest.raises(rich.GenerationError, match="offline verification"):
        rich.verify_generation(current, require_full_gate=True)

    pointer = json.loads(store.pointer_path.read_text())
    pointer["generationId"] = "tampered"
    rich.atomic_json(store.pointer_path, pointer)
    with pytest.raises(rich.GenerationError, match="checksum"):
        store.resolve_current()


def test_generation_verifier_rejects_raw_body_tamper_even_when_marker_survives(tmp_path):
    store = make_store(tmp_path)
    record = normalize(message(components=[{"type": 10, "content": "visible"}]))
    write_initial_generation(store, tmp_path, record)
    current = store.resolve_current()
    raw = current / "raw/2026-09-05.md"
    body = raw.read_text().replace("visible", "tampered", 1)
    rich._atomic_bytes(raw, body.encode())
    manifest = rich.generation_inventory(current)
    rich.atomic_json(current / "generation-manifest.json", manifest)
    result = rich.verify_generation(current)
    assert result["markdownErrors"] == 1
    assert not result["verified"]


def test_real_full_pass_receipt_binds_non_self_referential_content_hash(tmp_path):
    store = make_store(tmp_path)
    record = normalize()
    run_context = full_run_context(tmp_path)
    try:
        lock_token = context_lock_token(run_context)
        stage = store.create_stage(
            "full-pass",
            copy_current=False,
            lock_token=lock_token,
            run_context=run_context,
        )
        rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [record])
        rich._atomic_bytes(stage / "raw/2026-09-05.md", rich.render_day([record]).encode())
        store.reserve_full_stage_assets(
            stage,
            run_context=run_context,
            channel_id="1490000000000000001",
            relative_path="test/entry",
            lock_token=lock_token,
        )
        with live_evidence(store, "full-pass", run_context) as evidence_token:
            installed = store.install_full_pass_evidence(
                stage,
                live_evidence_token=evidence_token,
                lock_token=lock_token,
            )
            receipt = installed["receipt"]
            assert receipt["gateStatus"] == "PASS"
            assert installed["auditReceipt"]["gateStatus"] == "AUDIT_ONLY"
            assert receipt["coverageCounts"]["messages"] == {"expected": 1, "verified": 1}
            assert receipt["contentGenerationSha256"] == rich.generation_inventory(stage)["contentGenerationSha256"]
            assert rich.verify_full_generation(
                stage, live_evidence_token=evidence_token,
            )["verified"]
            with pytest.raises(rich.GenerationError, match="offline verification"):
                rich.verify_generation(stage, require_full_gate=True)
            store.publish_stage(
                stage,
                "full-pass",
                installed["manifest"]["generationSha256"],
                require_full_gate=True,
                live_evidence_token=evidence_token,
                lock_token=lock_token,
            )
            assert evidence_token.closed
            with pytest.raises(rich.GenerationError, match="runtime live evidence token"):
                rich.verify_full_generation(
                    store.resolve_current(), live_evidence_token=evidence_token,
                )
        assert store.resolve_current().name == "full-pass"
        assert rich.finalize_full_rebuild_run(run_context)["gateStatus"] == "PASS"
    finally:
        run_context.close()


def test_root_atomic_full_stage_seals_before_compatibility_current(tmp_path):
    store = make_store(tmp_path)
    run_context = full_run_context(tmp_path)
    try:
        lock_token = context_lock_token(run_context)
        evidence_token = live_evidence(store, "root-sealed", run_context)
        stage = store.materialize_full_stage_from_live_evidence(
            generation_id="root-sealed",
            live_evidence_token=evidence_token,
            downloader=rich.AssetDownloader(),
            lock_token=lock_token,
        )
        store.reserve_full_stage_assets(
            stage,
            run_context=run_context,
            channel_id="1490000000000000001",
            relative_path="test/entry",
            lock_token=lock_token,
        )
        installed = store.install_full_pass_evidence(
            stage,
            live_evidence_token=evidence_token,
            lock_token=lock_token,
        )
        generation_sha256 = installed["manifest"]["generationSha256"]
        final = store.seal_full_stage_for_root_run(
            stage,
            "root-sealed",
            generation_sha256,
            live_evidence_token=evidence_token,
            lock_token=lock_token,
            run_context=run_context,
        )
        assert final.is_dir()
        assert store.resolve_current() is None
        sealed = rich.verify_sealed_full_rebuild_run(run_context)
        assert sealed["gateStatus"] == "PASS"
        pointer = store.publish_existing_generation_pointer(
            "root-sealed",
            generation_sha256,
            lock_token=lock_token,
            run_context=run_context,
        )
        assert pointer["generationId"] == "root-sealed"
        assert store.resolve_current() == final
    finally:
        run_context.close()


def test_root_atomic_resume_requires_fresh_matching_live_evidence(tmp_path):
    store = make_store(tmp_path)
    first_context = full_run_context(tmp_path)
    try:
        first_lock = context_lock_token(first_context)
        first_token = live_evidence(store, "root-resume", first_context)
        stage = store.materialize_full_stage_from_live_evidence(
            generation_id="root-resume",
            live_evidence_token=first_token,
            downloader=rich.AssetDownloader(),
            lock_token=first_lock,
        )
        store.reserve_full_stage_assets(
            stage,
            run_context=first_context,
            channel_id="1490000000000000001",
            relative_path="test/entry",
            lock_token=first_lock,
        )
        installed = store.install_full_pass_evidence(
            stage,
            live_evidence_token=first_token,
            lock_token=first_lock,
        )
        generation_sha256 = installed["manifest"]["generationSha256"]
        final = store.seal_full_stage_for_root_run(
            stage,
            "root-resume",
            generation_sha256,
            live_evidence_token=first_token,
            lock_token=first_lock,
            run_context=first_context,
        )
    finally:
        first_context.close()

    second_context = full_run_context(tmp_path)
    try:
        second_lock = context_lock_token(second_context)
        fresh_token = live_evidence(store, "root-resume", second_context)
        resumed = store.register_existing_sealed_generation_for_root_run(
            final,
            "root-resume",
            generation_sha256,
            live_evidence_token=fresh_token,
            lock_token=second_lock,
            run_context=second_context,
        )
        assert fresh_token.closed
        assert resumed["records"] == 1
        assert rich.verify_sealed_full_rebuild_run(second_context)["gateStatus"] == "PASS"
    finally:
        second_context.close()

    third_context = full_run_context(tmp_path)
    try:
        third_lock = context_lock_token(third_context)
        changed_token = live_evidence(
            store,
            "root-resume",
            third_context,
            [message(content="changed")],
        )
        with pytest.raises(rich.GenerationError, match="fresh Discord evidence"):
            store.register_existing_sealed_generation_for_root_run(
                final,
                "root-resume",
                generation_sha256,
                live_evidence_token=changed_token,
                lock_token=third_lock,
                run_context=third_context,
            )
        assert changed_token.closed
        assert rich.verify_sealed_full_rebuild_run(third_context)["gateStatus"] == "FAIL"
    finally:
        third_context.close()


def test_materialized_stage_resume_rebinds_only_signed_url_churn_without_redownload(tmp_path):
    store = make_store(tmp_path)
    limits = rich.AssetLimits(disk_reserve_bytes=0)
    first_source = message(attachments=[{
        "id": "900",
        "filename": "resume.bin",
        "size": 3,
        "url": "https://cdn.discordapp.com/attachments/1/resume.bin?ex=1&is=2&hm=aaa",
    }])
    opener = FakeOpener([FakeResponse(body=b"abc")])
    first_context = full_run_context(tmp_path, limits=limits)
    try:
        first_token = live_evidence(
            store, "materialized-resume", first_context, [first_source],
        )
        stage = store.materialize_full_stage_from_live_evidence(
            generation_id="materialized-resume",
            live_evidence_token=first_token,
            downloader=rich.AssetDownloader(
                opener=opener,
                resolver=global_resolver,
                limits=limits,
            ),
            lock_token=context_lock_token(first_context),
        )
    finally:
        first_context.close()

    assert [path.name for path in (stage / "receipts").iterdir()] == [
        "stage-base-current.json",
    ]
    second_source = json.loads(json.dumps(first_source))
    second_source["attachments"][0]["url"] = (
        "https://cdn.discordapp.com/attachments/1/resume.bin?ex=9&is=8&hm=bbb"
    )
    second_context = full_run_context(tmp_path, limits=limits)
    try:
        lock_token = context_lock_token(second_context)
        fresh_token = live_evidence(
            store, "materialized-resume", second_context, [second_source],
        )
        rebound = store.rebind_materialized_full_stage_from_live_evidence(
            stage,
            live_evidence_token=fresh_token,
            lock_token=lock_token,
        )
        assert rebound["records"] == 1
        assert len(opener.requests) == 1
        store.reserve_full_stage_assets(
            stage,
            run_context=second_context,
            channel_id="1490000000000000001",
            relative_path="test/entry",
            lock_token=lock_token,
        )
        installed = store.install_full_pass_evidence(
            stage,
            live_evidence_token=fresh_token,
            lock_token=lock_token,
        )
        final = store.seal_full_stage_for_root_run(
            stage,
            "materialized-resume",
            installed["manifest"]["generationSha256"],
            live_evidence_token=fresh_token,
            lock_token=lock_token,
            run_context=second_context,
        )
        assert final.is_dir()
        assert fresh_token.closed
        assert rich.verify_sealed_full_rebuild_run(second_context)["gateStatus"] == "PASS"
    finally:
        second_context.close()


def test_materialized_stage_resume_rejects_semantic_asset_change(tmp_path):
    store = make_store(tmp_path)
    limits = rich.AssetLimits(disk_reserve_bytes=0)
    source = message(attachments=[{
        "id": "900",
        "filename": "resume.bin",
        "size": 3,
        "url": "https://cdn.discordapp.com/attachments/1/resume.bin?ex=1&is=2&hm=aaa",
    }])
    first_context = full_run_context(tmp_path, limits=limits)
    try:
        token = live_evidence(store, "semantic-change", first_context, [source])
        stage = store.materialize_full_stage_from_live_evidence(
            generation_id="semantic-change",
            live_evidence_token=token,
            downloader=rich.AssetDownloader(
                opener=FakeOpener([FakeResponse(body=b"abc")]),
                resolver=global_resolver,
                limits=limits,
            ),
            lock_token=context_lock_token(first_context),
        )
    finally:
        first_context.close()

    changed = json.loads(json.dumps(source))
    changed["attachments"][0]["filename"] = "different.bin"
    second_context = full_run_context(tmp_path, limits=limits)
    try:
        token = live_evidence(store, "semantic-change", second_context, [changed])
        with pytest.raises(rich.GenerationError, match="fresh Discord evidence"):
            store.rebind_materialized_full_stage_from_live_evidence(
                stage,
                live_evidence_token=token,
                lock_token=context_lock_token(second_context),
            )
        assert token.closed
        assert not (stage / "receipts/full-run-asset-reservation.json").exists()
        assert not (stage / "receipts/rich-archive-latest.json").exists()
    finally:
        second_context.close()


def test_materialized_stage_resume_rejects_tampered_asset_bytes(tmp_path):
    store = make_store(tmp_path)
    limits = rich.AssetLimits(disk_reserve_bytes=0)
    source = message(attachments=[{
        "id": "900",
        "filename": "resume.bin",
        "size": 3,
        "url": "https://cdn.discordapp.com/attachments/1/resume.bin",
    }])
    first_context = full_run_context(tmp_path, limits=limits)
    try:
        token = live_evidence(store, "tampered-stage", first_context, [source])
        stage = store.materialize_full_stage_from_live_evidence(
            generation_id="tampered-stage",
            live_evidence_token=token,
            downloader=rich.AssetDownloader(
                opener=FakeOpener([FakeResponse(body=b"abc")]),
                resolver=global_resolver,
                limits=limits,
            ),
            lock_token=context_lock_token(first_context),
        )
    finally:
        first_context.close()

    record = next(iter(rich._load_generation_records(stage)[0].values()))
    asset = record["observations"][0]["assetInventory"][0]
    rich.contained_path(stage, asset["localRelativePath"]).write_bytes(b"xyz")
    second_context = full_run_context(tmp_path, limits=limits)
    try:
        token = live_evidence(store, "tampered-stage", second_context, [source])
        with pytest.raises(
            (rich.GenerationError, rich.AssetDownloadError),
            match="local verification|verified local attachment bytes",
        ):
            store.rebind_materialized_full_stage_from_live_evidence(
                stage,
                live_evidence_token=token,
                lock_token=context_lock_token(second_context),
            )
        assert token.closed
        assert not (stage / "receipts/full-run-asset-reservation.json").exists()
    finally:
        second_context.close()


def test_cross_generation_install_rejects_and_consumes_bound_token(tmp_path):
    store = make_store(tmp_path)
    with full_run_context(tmp_path) as run_context:
        lock_token = context_lock_token(run_context)
        bound_stage = store.create_stage(
            "bound", copy_current=False, lock_token=lock_token, run_context=run_context,
        )
        wrong_stage = store.create_stage(
            "wrong", copy_current=False, lock_token=lock_token, run_context=run_context,
        )
        token = live_evidence(store, "bound", run_context, [])
        with pytest.raises(rich.GenerationError, match="another transaction"):
            store.install_full_pass_evidence(
                wrong_stage,
                live_evidence_token=token,
                lock_token=lock_token,
            )
        assert token.closed
        with pytest.raises(rich.GenerationError, match="runtime live evidence token"):
            store.install_full_pass_evidence(
                bound_stage,
                live_evidence_token=token,
                lock_token=lock_token,
            )


def test_publish_failure_consumes_token_and_blocks_replay(tmp_path):
    store = make_store(tmp_path)
    record = normalize()
    with full_run_context(tmp_path) as run_context:
        lock_token = context_lock_token(run_context)
        stage = store.create_stage(
            "publish-failure",
            copy_current=False,
            lock_token=lock_token,
            run_context=run_context,
        )
        rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [record])
        rich._atomic_bytes(stage / "raw/2026-09-05.md", rich.render_day([record]).encode())
        store.reserve_full_stage_assets(
            stage,
            run_context=run_context,
            channel_id="1490000000000000001",
            relative_path="test/entry",
            lock_token=lock_token,
        )
        token = live_evidence(store, "publish-failure", run_context)
        installed = store.install_full_pass_evidence(
            stage,
            live_evidence_token=token,
            lock_token=lock_token,
        )
        with pytest.raises(rich.GenerationError, match="prepared for this generation hash"):
            store.publish_stage(
                stage,
                "publish-failure",
                "0" * 64,
                require_full_gate=True,
                live_evidence_token=token,
                lock_token=lock_token,
            )
        assert token.closed
        with pytest.raises(rich.GenerationError, match="runtime live evidence token"):
            store.publish_stage(
                stage,
                "publish-failure",
                installed["manifest"]["generationSha256"],
                require_full_gate=True,
                live_evidence_token=token,
                lock_token=lock_token,
            )


def test_forged_pass_receipt_is_rejected(tmp_path):
    store = make_store(tmp_path)
    record = normalize()
    with incremental_run_context(tmp_path) as run_context:
        stage = store.create_stage(
            "forged",
            copy_current=False,
            run_context=run_context,
        )
    rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [record])
    rich._atomic_bytes(stage / "raw/2026-09-05.md", rich.render_day([record]).encode())
    rich.atomic_json(stage / "receipts/rich-archive-latest.json", {
        "schemaVersion": rich.ENTRY_RECEIPT_SCHEMA,
        "gateStatus": "PASS",
        "inventoryCoverage": 99,
        "idCoverage": 100,
        "visibleTextCoverage": 100,
        "markdownCoverage": 100,
        "binaryAssetCoverage": 100,
        "liveErrors": 0,
        "duplicateCanonicalIds": 0,
        "unknownVisibleFields": 0,
        "attachmentErrors": 0,
        "inventoryComplete": True,
        "inventoryDigest": "a" * 64,
        "verifiedCutoff": "1540000000000000001",
        "immutableEvidenceVerified": True,
        "contentGenerationSha256": "b" * 64,
    })
    rich.atomic_json(stage / "generation-manifest.json", rich.generation_inventory(stage))
    with pytest.raises(rich.GenerationError, match="self-assert live PASS"):
        rich.verify_generation(stage, require_full_gate=True)


def test_generation_journal_recovery_does_not_publish_unselected_generation(tmp_path):
    store = make_store(tmp_path)
    record = normalize()
    write_initial_generation(store, tmp_path, record)
    with incremental_run_context(tmp_path) as run_context:
        lock_token = context_lock_token(run_context)
        stage = store.create_stage(
            "next",
            copy_current=True,
            lock_token=lock_token,
            run_context=run_context,
        )
        manifest = rich.generation_inventory(stage)
        rich.atomic_json(stage / "generation-manifest.json", manifest)
        final = store.generations / "next"
        os.replace(stage, final)
        rich.atomic_json(store.journal_path, rich._journal_payload({
            "schemaVersion": rich.JOURNAL_SCHEMA,
            "phase": "generation_ready",
            "generationId": "next",
            "generationSha256": manifest["generationSha256"],
        }))

        outcome = store.recover_journal(
            lock_token=lock_token,
            run_context=run_context,
        )
    assert outcome["action"] == "retain_unpublished_generation"
    assert store.resolve_current().name == "initial"


def test_journal_checksum_and_generation_path_are_fail_closed(tmp_path):
    store = make_store(tmp_path)
    store.entry_root.mkdir(parents=True)
    rich.atomic_json(store.journal_path, {
        "schemaVersion": rich.JOURNAL_SCHEMA,
        "phase": "generation_ready",
        "generationId": "../escape",
        "generationSha256": "a" * 64,
        "journalSha256": "b" * 64,
    })
    with incremental_run_context(tmp_path) as run_context:
        with pytest.raises(rich.GenerationError):
            store.recover_journal(run_context=run_context)


def test_shared_lock_rejects_symlink_and_insecure_mode(tmp_path):
    target = tmp_path / "target"
    target.write_text("")
    lock = tmp_path / "lock"
    lock.symlink_to(target)
    with pytest.raises(rich.RichArchiveError):
        rich.RichArchiveStore(tmp_path / "entry", lock_path=lock).acquire_lock()
    lock.unlink()
    lock.write_text("")
    lock.chmod(0o644)
    with pytest.raises(rich.RichArchiveError, match="mode gate"):
        rich.RichArchiveStore(tmp_path / "entry", lock_path=lock).acquire_lock()


def test_store_atomic_merge_is_idempotent_and_requires_existing_full_rebuild(tmp_path):
    empty = make_store(tmp_path, "empty")
    with incremental_run_context(
        tmp_path,
        entries=[inventory_entry(relative_path="empty")],
    ) as run_context:
        with pytest.raises(rich.GenerationError, match="full rebuild"):
            empty.merge_messages(
                [message()], channel_id="1490000000000000001",
                relative_path="empty", observed_at=OBSERVED,
                generation_id="x", run_context=run_context,
            )

    store = make_store(tmp_path)
    write_initial_generation(store, tmp_path, normalize())
    newer = message(content="edited", edited_timestamp="2026-09-05T04:30:00Z")
    with incremental_run_context(tmp_path) as run_context:
        result = store.merge_messages(
            [newer], channel_id="1490000000000000001", relative_path="test/entry",
            observed_at="2026-09-05T04:31:00Z", generation_id="next",
            run_context=run_context,
        )
    assert result["gateStatus"] == "INCOMPLETE"
    current = store.resolve_current()
    rows = rich.load_jsonl(current / "canonical/2026-09-05.jsonl")
    assert len(rows) == 1
    assert len(rows[0]["contentRevisions"]) == 2
    assert rich.parse_markdown_markers((current / "raw/2026-09-05.md").read_text()) == [
        (rows[0]["messageId"], rows[0]["visiblePayloadSha256"])
    ]


def test_unknown_top_level_root_is_accounted_rendered_and_fails_closed():
    record = normalize(message(future_visible_root={"text": "must not disappear"}))
    census = record["sourceCensus"]
    assert census["unclassifiedPointers"] == ["/future_visible_root/text"]
    observation = record["observations"][0]
    accounting = next(
        row for row in observation["rendererAccounting"]
        if row["pointer"] == "/future_visible_root/text"
    )
    assert accounting == {
        "pointer": "/future_visible_root/text",
        "valueSha256": rich.json_sha256("must not disappear"),
        "rendererSection": "未分類可見資料",
    }
    rendered = rich.render_message(record)
    assert "[未分類可見資料]" in rendered
    assert "must not disappear" in rendered
    assert not rich.validate_record(record, require_assets=False)["ok"]


def test_external_original_and_discord_proxy_are_separate_asset_denominator_rows():
    record = normalize(message(attachments=[{
        "id": "900",
        "filename": "proxied.png",
        "size": 3,
        "url": "https://example.com/original.png",
        "proxy_url": "https://media.discordapp.net/attachments/1/proxied.png",
    }]))
    assets = record["observations"][0]["assetInventory"]
    assert len(assets) == 2
    original = next(row for row in assets if row["kind"] == "attachment")
    proxy = next(row for row in assets if row["kind"] == "attachment_proxy")
    assert original["status"] == "metadata_only" and not original["inScope"]
    assert proxy["status"] == "pending" and proxy["inScope"]
    assert proxy["localRelativePath"]


def test_both_sticker_shapes_are_rendered():
    record = normalize(message(
        content="",
        sticker_items=[{"id": "1500000000000000001", "name": "item sticker", "format_type": 1}],
        stickers=[{
            "id": "1500000000000000002",
            "name": "full sticker",
            "format_type": 1,
            "url": "https://cdn.discordapp.com/stickers/1500000000000000002.png",
        }],
    ))
    rendered = rich.render_message(record)
    assert "item sticker" in rendered
    assert "full sticker" in rendered


def test_active_ids_must_be_latest_and_equal_timestamp_conflicts_fail_closed():
    first = normalize(message(content="v1"), observed_at="2026-09-05T04:00:00Z")
    second = normalize(
        message(content="v2", edited_timestamp="2026-09-05T04:10:00Z"),
        observed_at="2026-09-05T04:11:00Z",
    )
    merged = rich.merge_message_records(first, second)
    merged["activeRevisionId"] = first["activeRevisionId"]
    with pytest.raises(rich.RichArchiveError, match="active revision is not the latest"):
        rich.validate_record(merged, require_assets=False)

    conflicting = normalize(message(content="different-without-edit-timestamp"))
    with pytest.raises(rich.RecordConflictError, match="share one versionTimestamp"):
        rich.merge_message_records(first, conflicting)


def test_historical_observation_render_preserves_full_source_payload():
    first_source = message(attachments=[{
        "id": "900",
        "filename": "x.png",
        "size": 3,
        "url": "https://cdn.discordapp.com/attachments/1/x.png?ex=1&hm=old",
    }])
    second_source = json.loads(json.dumps(first_source))
    second_source["attachments"][0]["url"] = (
        "https://cdn.discordapp.com/attachments/1/x.png?ex=2&hm=new"
    )
    merged = rich.merge_message_records(
        normalize(first_source, observed_at="2026-09-05T04:00:00Z"),
        normalize(second_source, observed_at="2026-09-05T04:10:00Z"),
    )
    rendered = rich.render_message(merged)
    assert "[歷史觀測版本]" in rendered
    assert "ex=1&hm=old" in rendered
    assert "ex=2&hm=new" in rendered


@pytest.mark.parametrize(
    "generation_id",
    [".staging-repair", ".hidden", "a.b", "CURRENT", "generations", "staging"],
)
def test_reserved_generation_ids_are_rejected(generation_id, tmp_path):
    store = make_store(tmp_path)
    with incremental_run_context(tmp_path) as run_context:
        with pytest.raises(rich.GenerationError, match="unsafe"):
            store.create_stage(
                generation_id,
                copy_current=False,
                run_context=run_context,
            )


def test_mutation_apis_require_live_matching_lock_token(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(rich.RichArchiveError, match="run context"):
        store.create_stage("one", copy_current=False)
    with pytest.raises(rich.RichArchiveError, match="run context"):
        store.recover_journal()

    token = store.acquire_lock()
    token.close()
    with pytest.raises(rich.RichArchiveError, match="held backup lock token"):
        rich.begin_incremental_run(
            entries=[inventory_entry()],
            archive_root=tmp_path,
            lock_token=token,
        )

    other = rich.RichArchiveStore(
        tmp_path / "other-entry",
        lock_path=tmp_path / "other.lock",
    )
    with incremental_run_context(
        tmp_path,
        entries=[inventory_entry(relative_path="other-entry")],
    ) as run_context:
        with pytest.raises(rich.RichArchiveError, match="canonical backup lock"):
            other.create_stage(
                "wrong-lock",
                copy_current=False,
                run_context=run_context,
            )
        with pytest.raises(rich.RichArchiveError, match="canonical backup lock"):
            other.publish_stage(
                tmp_path / "nonexistent-stage",
                "wrong-lock-publish",
                "0" * 64,
                run_context=run_context,
            )
        with pytest.raises(rich.RichArchiveError, match="canonical backup lock"):
            other.recover_journal(run_context=run_context)
    assert not other.entry_root.exists()


def test_public_create_stage_requires_run_capability_before_filesystem_mutation(tmp_path):
    store = make_store(tmp_path)
    with store.acquire_lock() as lock_token:
        with pytest.raises(rich.RichArchiveError, match="run context"):
            store.create_stage(
                "unscoped-create",
                copy_current=False,
                lock_token=lock_token,
                run_context=None,
            )
    assert not store.entry_root.exists()


def test_public_publish_stage_requires_run_capability_before_managed_mutation(tmp_path):
    store = make_store(tmp_path)
    with store.acquire_lock() as lock_token:
        lease = store._begin_lock_lease(lock_token)
        try:
            stage = store._create_stage_under_lease(
                "unscoped-publish",
                copy_current=False,
                lock_token=lock_token,
            )
        finally:
            store._end_lock_lease(lease)
        rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [normalize()])
        rich._atomic_bytes(
            stage / "raw/2026-09-05.md",
            rich.render_day([normalize()]).encode(),
        )
        rich.atomic_json(stage / "receipts/rich-archive-latest.json", {
            "schemaVersion": rich.ENTRY_RECEIPT_SCHEMA,
            "gateStatus": "INCOMPLETE",
            "reason": "capability-regression",
        })
        manifest = rich.generation_inventory(stage)
        rich.atomic_json(stage / "generation-manifest.json", manifest)
        with pytest.raises(rich.RichArchiveError, match="run context"):
            store.publish_stage(
                stage,
                "unscoped-publish",
                manifest["generationSha256"],
                lock_token=lock_token,
                run_context=None,
            )
    assert not store.pointer_path.exists()
    assert not store.journal_path.exists()
    assert not (store.generations / "unscoped-publish").exists()


def test_public_recover_journal_requires_run_capability_before_mutation(tmp_path):
    store = make_store(tmp_path)
    with store.acquire_lock() as lock_token:
        with pytest.raises(rich.RichArchiveError, match="run context"):
            store.recover_journal(lock_token=lock_token, run_context=None)
    assert not store.entry_root.exists()


def test_unregistered_same_inode_lock_token_is_rejected_even_with_private_guard(tmp_path):
    store = make_store(tmp_path)
    store.lock_path.touch(mode=0o600)
    forged = rich.ArchiveLockToken(guard=rich._LOCK_TOKEN_GUARD)
    try:
        with incremental_run_context(tmp_path) as run_context:
            with pytest.raises(rich.RichArchiveError, match="bound lock token"):
                store.create_stage(
                    "forged-lock",
                    copy_current=False,
                    lock_token=forged,
                    run_context=run_context,
                )
    finally:
        forged.close()


def test_registered_lock_token_object_identity_cannot_be_copied(tmp_path):
    store = make_store(tmp_path)
    with incremental_run_context(tmp_path) as run_context:
        valid = context_lock_token(run_context)
        for forbidden in ("path", "_path", "handle", "_handle", "descriptor", "_descriptor", "fd", "_fd"):
            assert not hasattr(valid, forbidden)
        copied = object.__new__(rich.ArchiveLockToken)
        copied._nonce = valid._nonce
        copied._pid = valid._pid
        copied._closed = valid._closed
        try:
            with pytest.raises(rich.RichArchiveError, match="bound lock token"):
                store.create_stage(
                    "copied-lock",
                    copy_current=False,
                    lock_token=copied,
                    run_context=run_context,
                )
        finally:
            copied.close()


def test_symlinked_entry_ancestor_is_rejected_before_archive_mutation(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with archive_run_lock(tmp_path) as lock_token:
        with pytest.raises(rich.RichArchiveError, match="symlinked"):
            rich.begin_incremental_run(
                entries=[inventory_entry(relative_path="linked/entry")],
                archive_root=tmp_path,
                lock_token=lock_token,
            )
    assert not (outside / "entry").exists()


def test_symlinked_download_root_is_rejected_before_network_or_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    opener = FakeOpener([])
    downloader = rich.AssetDownloader(opener=opener, resolver=global_resolver)
    with pytest.raises(rich.RichArchiveError, match="symlinked"):
        downloader.download(pending_asset(), linked)
    assert opener.requests == []
    assert list(outside.iterdir()) == []


def test_full_pass_receipt_recomputes_counts_and_rejects_tampering(tmp_path):
    store = make_store(tmp_path)
    record = normalize()
    with full_run_context(tmp_path) as run_context:
        lock_token = context_lock_token(run_context)
        stage = store.create_stage(
            "tamper-pass",
            copy_current=False,
            lock_token=lock_token,
            run_context=run_context,
        )
        rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [record])
        rich._atomic_bytes(stage / "raw/2026-09-05.md", rich.render_day([record]).encode())
        store.reserve_full_stage_assets(
            stage,
            run_context=run_context,
            channel_id="1490000000000000001",
            relative_path="test/entry",
            lock_token=lock_token,
        )
        with live_evidence(store, "tamper-pass", run_context) as evidence_token:
            installed = store.install_full_pass_evidence(
                stage,
                live_evidence_token=evidence_token,
                lock_token=lock_token,
            )
            receipt_path = stage / "receipts/rich-archive-latest.json"
            forged = dict(installed["receipt"])
            forged["coverageCounts"] = dict(forged["coverageCounts"])
            forged["coverageCounts"]["messages"] = {"expected": 0, "verified": 0}
            rich.atomic_json(receipt_path, forged)
            rich.atomic_json(stage / "generation-manifest.json", rich.generation_inventory(stage))
            with pytest.raises(rich.GenerationError):
                rich.verify_full_generation(stage, live_evidence_token=evidence_token)


@pytest.mark.parametrize("mutation", ["identity", "cutoff", "fingerprint", "immutable"])
def test_live_evidence_identity_cutoff_fingerprint_and_immutable_binding_fail_closed(mutation, tmp_path):
    store = make_store(tmp_path)
    record = normalize()
    with full_run_context(tmp_path) as run_context:
        lock_token = context_lock_token(run_context)
        stage = store.create_stage(
            f"bad-{mutation}",
            copy_current=False,
            lock_token=lock_token,
            run_context=run_context,
        )
        rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [record])
        rich._atomic_bytes(stage / "raw/2026-09-05.md", rich.render_day([record]).encode())
        store.reserve_full_stage_assets(
            stage,
            run_context=run_context,
            channel_id="1490000000000000001",
            relative_path="test/entry",
            lock_token=lock_token,
        )
        with live_evidence(store, f"bad-{mutation}", run_context) as evidence_token:
            installed = store.install_full_pass_evidence(
                stage,
                live_evidence_token=evidence_token,
                lock_token=lock_token,
            )
            evidence_path = stage / "receipts/live-inventory-evidence.json"
            evidence = json.loads(evidence_path.read_text())
            if mutation == "identity":
                evidence["entryIdentity"] = {}
            elif mutation == "cutoff":
                evidence["verifiedCutoff"] = "1540000000000000000"
            elif mutation == "fingerprint":
                evidence["messages"][0]["visiblePayloadSha256"] = "f" * 64
            else:
                evidence["immutableEvidence"]["stateSha256"] = "invalid"
            evidence.pop("evidenceSha256", None)
            evidence["evidenceSha256"] = rich.json_sha256(evidence)
            rich.atomic_json(evidence_path, evidence)
            rich.atomic_json(stage / "generation-manifest.json", rich.generation_inventory(stage))
            with pytest.raises(rich.GenerationError):
                rich.verify_full_generation(stage, live_evidence_token=evidence_token)
            assert installed["receipt"]["gateStatus"] == "PASS"


def test_truly_empty_full_pass_requires_concrete_terminal_inventory_evidence(tmp_path):
    store = make_store(tmp_path)
    with full_run_context(tmp_path) as run_context:
        lock_token = context_lock_token(run_context)
        stage = store.create_stage(
            "empty-pass",
            copy_current=False,
            lock_token=lock_token,
            run_context=run_context,
        )
        store.reserve_full_stage_assets(
            stage,
            run_context=run_context,
            channel_id="1490000000000000001",
            relative_path="test/entry",
            lock_token=lock_token,
        )
        with live_evidence(store, "empty-pass", run_context, []) as evidence_token:
            installed = store.install_full_pass_evidence(
                stage,
                live_evidence_token=evidence_token,
                lock_token=lock_token,
            )
            assert installed["receipt"]["gateStatus"] == "PASS"
            assert installed["receipt"]["coverageCounts"]["messages"] == {
                "expected": 0,
                "verified": 0,
            }

            evidence_path = stage / "receipts/live-inventory-evidence.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["enumeration"]["terminalPageObserved"] = False
            evidence.pop("evidenceSha256", None)
            evidence["evidenceSha256"] = rich.json_sha256(evidence)
            rich.atomic_json(evidence_path, evidence)
            rich.atomic_json(stage / "generation-manifest.json", rich.generation_inventory(stage))
            with pytest.raises(rich.GenerationError):
                rich.verify_full_generation(stage, live_evidence_token=evidence_token)


def test_incomplete_full_run_receipt_fails_and_consumes_context(tmp_path):
    run_context = full_run_context(tmp_path, entries=[
        inventory_entry(channel_id="1490000000000000001", relative_path="test/one"),
        inventory_entry(channel_id="1490000000000000002", relative_path="test/two"),
    ])
    receipt = rich.finalize_full_rebuild_run(run_context)
    assert receipt["schemaVersion"] == rich.FULL_RUN_RECEIPT_SCHEMA
    assert receipt["gateStatus"] == "FAIL"
    assert receipt["expectedEntryCount"] == 2
    assert receipt["processedEntryCount"] == 0
    assert "not_all_inventory_entries_published" in receipt["errors"]
    assert "not_all_asset_reservations_consumed" in receipt["errors"]
    assert run_context.closed
    with pytest.raises(rich.RichArchiveError, match="run context"):
        rich.finalize_full_rebuild_run(run_context)


@pytest.mark.parametrize("close_target", ["context", "bound_lock_token"])
def test_finalize_holds_run_and_lock_until_readback_decision(
    monkeypatch,
    tmp_path,
    close_target,
):
    store = make_store(tmp_path)
    write_initial_generation(store, tmp_path, normalize())
    run_context = full_run_context(tmp_path)
    lock_token = context_lock_token(run_context)
    entered = threading.Event()
    release = threading.Event()
    receipts = []
    errors = []
    original_resolve_current = rich.RichArchiveStore.resolve_current

    def blocking_resolve_current(self):
        if self.entry_root == store.entry_root and threading.current_thread().name == "finalizer":
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("finalize readback gate timed out")
        return original_resolve_current(self)

    monkeypatch.setattr(
        rich.RichArchiveStore,
        "resolve_current",
        blocking_resolve_current,
    )

    def finalize():
        try:
            receipts.append(rich.finalize_full_rebuild_run(run_context))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=finalize, name="finalizer")
    worker.start()
    deferred_close = False
    second_run_rejected = False
    try:
        assert entered.wait(timeout=10)
        if close_target == "context":
            run_context.close()
        else:
            lock_token.close()
        deferred_close = not run_context.closed and not lock_token.closed
        try:
            second_context = incremental_run_context(tmp_path)
        except rich.RichArchiveError:
            second_run_rejected = True
        else:
            second_context.close()
    finally:
        release.set()
        worker.join(timeout=10)
        run_context.close()

    assert not worker.is_alive()
    assert deferred_close
    assert second_run_rejected
    assert not errors
    assert len(receipts) == 1
    assert receipts[0]["gateStatus"] == "FAIL"
    assert run_context.closed
    assert lock_token.closed
    with incremental_run_context(tmp_path):
        pass


def test_merge_requires_module_minted_run_context_without_leaking_the_shared_lock(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(TypeError, match="run_context"):
        store.merge_messages(
            [message()],
            channel_id="1490000000000000001",
            relative_path="test/entry",
            observed_at=OBSERVED,
            generation_id="missing-budget",
        )
    with pytest.raises(rich.RichArchiveError, match="module-minted archive run context"):
        store.merge_messages(
            [message()],
            channel_id="1490000000000000001",
            relative_path="test/entry",
            observed_at=OBSERVED,
            generation_id="invalid-budget",
            run_context=None,
        )
    with store.acquire_lock():
        pass


def test_one_asset_budget_is_consumed_across_two_entry_merges(tmp_path):
    first_store = make_store(tmp_path, "entry-one")
    second_store = make_store(tmp_path, "entry-two")
    write_initial_generation(first_store, tmp_path, normalize())
    write_initial_generation(
        second_store,
        tmp_path,
        normalize(message(channel_id="1490000000000000002")),
    )

    limits = rich.AssetLimits(
        full_run_files=1,
        full_run_bytes=3,
        disk_reserve_bytes=0,
    )
    opener = FakeOpener([FakeResponse(body=b"abc")])
    downloader = rich.AssetDownloader(
        opener=opener,
        resolver=global_resolver,
        limits=limits,
    )
    first = message(id="1540000000000000002", attachments=[{
        "id": "901",
        "filename": "first.bin",
        "size": 3,
        "url": "https://cdn.discordapp.com/attachments/1/first.bin",
    }])
    second = message(id="1540000000000000003", channel_id="1490000000000000002", attachments=[{
        "id": "902",
        "filename": "second.bin",
        "size": 3,
        "url": "https://cdn.discordapp.com/attachments/1/second.bin",
    }])

    run_context = full_run_context(
        tmp_path,
        entries=[
            inventory_entry(channel_id="1490000000000000001", relative_path="entry-one"),
            inventory_entry(channel_id="1490000000000000002", relative_path="entry-two"),
        ],
        limits=limits,
    )
    try:
        first_store.merge_messages(
            [first],
            channel_id="1490000000000000001",
            relative_path="entry-one",
            observed_at=OBSERVED,
            generation_id="entry-one-next",
            downloader=downloader,
            run_context=run_context,
        )
        with pytest.raises(rich.AssetDownloadError, match="file quota"):
            second_store.merge_messages(
                [second],
                channel_id="1490000000000000002",
                relative_path="entry-two",
                observed_at=OBSERVED,
                generation_id="entry-two-next",
                downloader=downloader,
                run_context=run_context,
            )
        receipt = rich.finalize_full_rebuild_run(run_context)
        assert receipt["gateStatus"] == "FAIL"
        assert receipt["assetFileCount"] == 1
    finally:
        run_context.close()
    assert len(opener.requests) == 1


def _write_incomplete_stage(store, generation_id, record, lock_token, run_context):
    stage = store.create_stage(
        generation_id,
        copy_current=False,
        lock_token=lock_token,
        run_context=run_context,
    )
    rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [record])
    rich._atomic_bytes(stage / "raw/2026-09-05.md", rich.render_day([record]).encode())
    rich.atomic_json(stage / "receipts/rich-archive-latest.json", {
        "schemaVersion": rich.ENTRY_RECEIPT_SCHEMA,
        "gateStatus": "INCOMPLETE",
        "reason": "regression-test",
    })
    manifest = rich.generation_inventory(stage)
    rich.atomic_json(stage / "generation-manifest.json", manifest)
    return stage, manifest


def test_full_rebuild_binds_expected_inventory_and_canonical_entry_root(tmp_path):
    expected = [inventory_entry()]
    wrong_inventory = [
        inventory_entry(
            channel_id="1490000000000000002",
            relative_path="test/other",
        ),
    ]
    with archive_run_lock(tmp_path) as lock_token:
        with pytest.raises(rich.GenerationError, match="independent expected entry set"):
            rich.begin_full_rebuild_run(
                fetch_inventory=lambda: inventory_response(wrong_inventory),
                expected_entries=expected,
                archive_root=tmp_path,
                lock_token=lock_token,
            )

    wrong_store = make_store(tmp_path, "wrong/archive-root")
    with full_run_context(tmp_path, expected) as run_context:
        with pytest.raises(rich.RichArchiveError, match="entry root"):
            live_evidence(
                wrong_store,
                "wrong-root",
                run_context,
                [],
                relative_path="test/entry",
            )
        with pytest.raises(rich.RichArchiveError, match="entry root"):
            wrong_store.merge_messages(
                [message()],
                channel_id="1490000000000000001",
                relative_path="test/entry",
                observed_at=OBSERVED,
                generation_id="wrong-root-merge",
                run_context=run_context,
            )


def test_live_evidence_expiry_during_runtime_verify_blocks_first_mutation(monkeypatch, tmp_path):
    clock = {"value": 0.0}
    monkeypatch.setattr(rich.time, "monotonic", lambda: clock["value"])
    store = make_store(tmp_path)
    record = normalize()
    run_context = full_run_context(tmp_path)
    try:
        lock_token = context_lock_token(run_context)
        with store._borrow_lock(lock_token):
            stage, _manifest = _write_incomplete_stage(
                store, "expires-during-verify", record, lock_token, run_context,
            )
            store.reserve_full_stage_assets(
                stage,
                run_context=run_context,
                channel_id="1490000000000000001",
                relative_path="test/entry",
                lock_token=lock_token,
            )
            token = live_evidence(
                store,
                "expires-during-verify",
                run_context,
                evidence_ttl_seconds=5,
            )
            installed = store.install_full_pass_evidence(
                stage,
                live_evidence_token=token,
                lock_token=lock_token,
            )
            original_verify = rich.verify_generation
            calls = {"count": 0}

            def expire_on_inner_verify(root, *args, **kwargs):
                result = original_verify(root, *args, **kwargs)
                calls["count"] += 1
                if calls["count"] == 2:
                    clock["value"] = 6.0
                return result

            monkeypatch.setattr(rich, "verify_generation", expire_on_inner_verify)
            with pytest.raises(rich.GenerationError, match="expired"):
                store.publish_stage(
                    stage,
                    "expires-during-verify",
                    installed["manifest"]["generationSha256"],
                    require_full_gate=True,
                    live_evidence_token=token,
                    lock_token=lock_token,
                )
            assert calls["count"] >= 2
            assert token.closed
            assert not store.pointer_path.exists()
            assert not store.journal_path.exists()
            assert stage.is_dir()
    finally:
        run_context.close()


def test_lock_close_waits_for_inflight_publish_before_unlock(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    run_context = incremental_run_context(tmp_path)
    lock_token = context_lock_token(run_context)
    stage, manifest = _write_incomplete_stage(
        store, "close-during-publish", normalize(), lock_token, run_context,
    )
    original_verify = rich.verify_generation
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def blocking_verify(root, *args, **kwargs):
        if Path(root) == stage and not entered.is_set():
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("timed out waiting to release publish verifier")
        return original_verify(root, *args, **kwargs)

    monkeypatch.setattr(rich, "verify_generation", blocking_verify)

    def publish():
        try:
            store.publish_stage(
                stage,
                "close-during-publish",
                manifest["generationSha256"],
                lock_token=lock_token,
                run_context=run_context,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=publish)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        lock_token.close()
        assert not lock_token.closed
        with pytest.raises(rich.RichArchiveError, match="busy"):
            store.acquire_lock()
    finally:
        release.set()
        worker.join(timeout=5)
        run_context.close()
    assert not worker.is_alive()
    assert errors == []
    assert lock_token.closed
    assert store.resolve_current().name == "close-during-publish"
    with store.acquire_lock() as replacement:
        assert not replacement.closed


def test_materialized_stage_cannot_bypass_zero_full_run_asset_quota(tmp_path):
    store = make_store(tmp_path)
    source = message(attachments=[{
        "id": "900",
        "filename": "proof.bin",
        "size": 3,
        "url": "https://cdn.discordapp.com/attachments/1/proof.bin",
    }])
    downloader = rich.AssetDownloader(
        opener=FakeOpener([FakeResponse(body=b"abc")]),
        resolver=global_resolver,
        limits=rich.AssetLimits(disk_reserve_bytes=0),
    )
    limits = rich.AssetLimits(
        full_run_files=0,
        full_run_bytes=0,
        disk_reserve_bytes=0,
    )
    run_context = full_run_context(tmp_path, limits=limits)
    try:
        lock_token = context_lock_token(run_context)
        with store._borrow_lock(lock_token):
            stage = store.create_stage(
                "manual-materialized-asset",
                copy_current=False,
                lock_token=lock_token,
                run_context=run_context,
            )
            record = rich.apply_asset_results(normalize(source), downloader, stage)
            rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [record])
            rich._atomic_bytes(
                stage / "raw/2026-09-05.md", rich.render_day([record]).encode(),
            )
            with pytest.raises(rich.AssetDownloadError, match="file quota"):
                store.reserve_full_stage_assets(
                    stage,
                    run_context=run_context,
                    channel_id="1490000000000000001",
                    relative_path="test/entry",
                    lock_token=lock_token,
                )

            token = live_evidence(
                store,
                "manual-materialized-asset",
                run_context,
                [source],
            )
            with pytest.raises(rich.GenerationError, match="full PASS gate"):
                store.install_full_pass_evidence(
                    stage,
                    live_evidence_token=token,
                    lock_token=lock_token,
                )
            assert token.closed
            assert not store.pointer_path.exists()
    finally:
        run_context.close()


def test_same_lock_token_rejects_concurrent_cross_thread_mutation(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    run_context = incremental_run_context(tmp_path)
    lock_token = context_lock_token(run_context)
    stage, manifest = _write_incomplete_stage(
        store, "first-thread-publish", normalize(), lock_token, run_context,
    )
    original_verify = rich.verify_generation
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def blocking_verify(root, *args, **kwargs):
        if Path(root) == stage and not entered.is_set():
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("timed out waiting to release first mutation")
        return original_verify(root, *args, **kwargs)

    monkeypatch.setattr(rich, "verify_generation", blocking_verify)

    def publish():
        try:
            store.publish_stage(
                stage,
                "first-thread-publish",
                manifest["generationSha256"],
                lock_token=lock_token,
                run_context=run_context,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=publish)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(rich.RichArchiveError, match="another thread"):
            store.create_stage(
                "second-thread-stage",
                copy_current=False,
                lock_token=lock_token,
                run_context=run_context,
            )
    finally:
        release.set()
        worker.join(timeout=5)
        run_context.close()
    assert not worker.is_alive()
    assert errors == []
    assert store.resolve_current().name == "first-thread-publish"


def test_asset_reservation_schema_is_publicly_exported():
    assert "ASSET_RESERVATION_SCHEMA" in rich.__all__
    assert rich.ASSET_RESERVATION_SCHEMA == "openclaw-discord-full-run-asset-reservation.v1"


def test_run_context_close_waits_for_inflight_merge(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    write_initial_generation(store, tmp_path, normalize())
    run_context = incremental_run_context(tmp_path)
    lock_token = context_lock_token(run_context)
    original_resolve = rich.RichArchiveStore.resolve_current
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def blocking_resolve(self):
        if threading.current_thread().name == "run-close-merge" and not entered.is_set():
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("timed out waiting to release merge")
        return original_resolve(self)

    monkeypatch.setattr(rich.RichArchiveStore, "resolve_current", blocking_resolve)

    def merge():
        try:
            store.merge_messages(
                [message(id="1540000000000000002", content="survives deferred close")],
                channel_id="1490000000000000001",
                relative_path="test/entry",
                observed_at=OBSERVED,
                generation_id="run-close-merge",
                run_context=run_context,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=merge, name="run-close-merge")
    worker.start()
    try:
        assert entered.wait(timeout=5)
        run_context.close()
        assert not run_context.closed
        assert not lock_token.closed
        assert str(tmp_path.absolute()) in rich._ACTIVE_ARCHIVE_ROOT_RUNS
        with pytest.raises(rich.RichArchiveError, match="busy"):
            archive_run_lock(tmp_path)
    finally:
        release.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert errors == []
    assert run_context.closed
    assert lock_token.closed
    assert str(tmp_path.absolute()) not in rich._ACTIVE_ARCHIVE_ROOT_RUNS
    assert store.resolve_current().name == "run-close-merge"
    with incremental_run_context(tmp_path):
        pass


def test_open_run_pins_bound_lock_across_direct_token_close_request(tmp_path):
    store = make_store(tmp_path)
    write_initial_generation(store, tmp_path, normalize())
    run_context = incremental_run_context(tmp_path)
    lock_token = context_lock_token(run_context)
    try:
        lock_token.close()
        assert not lock_token.closed
        assert rich._require_run_context(run_context) is not None
        with pytest.raises(rich.RichArchiveError, match="busy"):
            archive_run_lock(tmp_path)
        result = store.merge_messages(
            [message(id="1540000000000000002", content="pinned run survives close request")],
            channel_id="1490000000000000001",
            relative_path="test/entry",
            observed_at=OBSERVED,
            generation_id="pinned-close-request",
            run_context=run_context,
        )
        assert result["verified"] is True
    finally:
        run_context.close()
    assert run_context.closed
    assert lock_token.closed
    assert store.resolve_current().name == "pinned-close-request"
    with incremental_run_context(tmp_path):
        pass


def test_finalize_removes_run_registry_before_last_lock_pin_release(
    monkeypatch,
    tmp_path,
):
    run_context = full_run_context(tmp_path)
    lock_token = context_lock_token(run_context)
    registration = rich._require_run_context(run_context)
    entered = threading.Event()
    release = threading.Event()
    receipts = []
    errors = []
    original_release_pin = rich._release_run_lock_pin

    def blocking_release_pin(pin, *, request_close=True):
        if pin is registration["lockRunPin"] and request_close:
            assert str(tmp_path.absolute()) not in rich._ACTIVE_ARCHIVE_ROOT_RUNS
            assert run_context.closed
            assert not lock_token.closed
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("run-pin cleanup gate timed out")
        return original_release_pin(pin, request_close=request_close)

    monkeypatch.setattr(rich, "_release_run_lock_pin", blocking_release_pin)
    lock_token.close()
    assert not lock_token.closed

    def finalize():
        try:
            receipts.append(rich.finalize_full_rebuild_run(run_context))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=finalize, name="run-pin-finalizer")
    worker.start()
    try:
        assert entered.wait(timeout=10)
        assert str(tmp_path.absolute()) not in rich._ACTIVE_ARCHIVE_ROOT_RUNS
        assert not lock_token.closed
        with pytest.raises(rich.RichArchiveError, match="busy"):
            archive_run_lock(tmp_path)
    finally:
        release.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert not errors
    assert len(receipts) == 1
    assert receipts[0]["gateStatus"] == "FAIL"
    assert run_context.closed
    assert lock_token.closed
    with incremental_run_context(tmp_path):
        pass


def test_run_context_exception_and_gc_release_active_registry(tmp_path):
    with pytest.raises(RuntimeError, match="synthetic run failure"):
        with incremental_run_context(tmp_path) as failed_context:
            failed_token = context_lock_token(failed_context)
            raise RuntimeError("synthetic run failure")
    assert failed_context.closed
    assert failed_token.closed

    leaked_context = incremental_run_context(tmp_path)
    leaked_token = context_lock_token(leaked_context)
    context_reference = weakref.ref(leaked_context)
    del leaked_context
    gc.collect()
    assert context_reference() is None
    assert leaked_token.closed
    assert str(tmp_path.absolute()) not in rich._ACTIVE_ARCHIVE_ROOT_RUNS
    with incremental_run_context(tmp_path):
        pass


def test_incremental_failure_after_asset_reserve_rolls_back_file_byte_budget(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    write_initial_generation(store, tmp_path, normalize())
    limits = rich.AssetLimits(disk_reserve_bytes=0)
    downloader = rich.AssetDownloader(
        opener=FakeOpener([FakeResponse(body=b"abc")]),
        resolver=global_resolver,
        limits=limits,
    )
    run_context = incremental_run_context(tmp_path, limits=limits)
    registration = rich._require_run_context(run_context, kind="incremental")

    def fail_after_reserve(*_args, **_kwargs):
        raise OSError("synthetic stage failure")

    monkeypatch.setattr(store, "create_stage", fail_after_reserve)
    try:
        with pytest.raises(OSError, match="synthetic stage failure"):
            store.merge_messages(
                [message(id="1540000000000000002", attachments=[{
                    "id": "900",
                    "filename": "proof.bin",
                    "size": 3,
                    "url": "https://cdn.discordapp.com/attachments/1/proof.bin",
                }])],
                channel_id="1490000000000000001",
                relative_path="test/entry",
                observed_at=OBSERVED,
                generation_id="fail-after-reserve",
                downloader=downloader,
                run_context=run_context,
            )
        assert registration["budget"].file_count == 0
        assert registration["budget"].declared_bytes == 0
    finally:
        run_context.close()


def test_incremental_run_cannot_lost_update_through_two_lock_paths(tmp_path):
    entry_root = tmp_path / "test/entry"
    store_a = rich.RichArchiveStore(entry_root, lock_path=tmp_path / LOCK_NAME)
    store_b = rich.RichArchiveStore(entry_root, lock_path=tmp_path / "lock-b")
    write_initial_generation(store_a, tmp_path, normalize())
    run_context = incremental_run_context(tmp_path)
    barrier = threading.Barrier(2)
    successes = []
    errors = []

    def merge(store, source, generation_id):
        try:
            barrier.wait(timeout=5)
            result = store.merge_messages(
                [source],
                channel_id="1490000000000000001",
                relative_path="test/entry",
                observed_at=OBSERVED,
                generation_id=generation_id,
                run_context=run_context,
            )
            successes.append((source["id"], result))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = message(id="1540000000000000002", content="first concurrent update")
    second = message(id="1540000000000000003", content="second concurrent update")
    threads = [
        threading.Thread(
            target=merge,
            name="lost-update-a",
            args=(store_a, first, "lost-update-a"),
        ),
        threading.Thread(
            target=merge,
            name="lost-update-b",
            args=(store_b, second, "lost-update-b"),
        ),
    ]
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join(timeout=10)
    try:
        assert all(not worker.is_alive() for worker in threads)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], rich.RichArchiveError)
        assert "lock" in str(errors[0]).lower()
        current = store_a.resolve_current()
        assert current is not None
        ids = {
            row["messageId"]
            for path in (current / "canonical").glob("*.jsonl")
            for row in rich.load_jsonl(path)
        }
        assert successes[0][0] in ids
    finally:
        run_context.close()


def test_cross_process_alternate_lock_cannot_publish_lost_update(tmp_path):
    store = make_store(tmp_path)
    write_initial_generation(store, tmp_path, normalize())
    context = multiprocessing.get_context("fork")
    release = context.Event()
    canonical_ready = context.Event()
    alternate_ready = context.Event()
    result_queue = context.Queue()
    canonical_message_id = "1540000000000000002"
    alternate_message_id = "1540000000000000003"
    workers = [
        context.Process(
            target=_cross_process_merge_worker,
            args=(
                str(tmp_path), LOCK_NAME, "canonical", canonical_message_id,
                "cross-process-canonical", canonical_ready, release, result_queue,
            ),
        ),
        context.Process(
            target=_cross_process_merge_worker,
            args=(
                str(tmp_path), "alternate.lock", "alternate", alternate_message_id,
                "cross-process-alternate", alternate_ready, release, result_queue,
            ),
        ),
    ]
    for worker in workers:
        worker.start()
    try:
        assert canonical_ready.wait(timeout=10)
        deadline = time.monotonic() + 10
        while workers[1].is_alive() and not alternate_ready.is_set():
            if time.monotonic() >= deadline:
                pytest.fail("alternate-lock worker did not reach a bounded decision")
            time.sleep(0.01)
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=10)
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)

    assert all(worker.exitcode == 0 for worker in workers)
    results = {}
    for _ in workers:
        try:
            row = result_queue.get(timeout=5)
        except queue.Empty:
            pytest.fail("multiprocess worker omitted its result")
        results[row[0]] = row[1:]
    assert results["canonical"] == ("ok", canonical_message_id, True)
    assert results["alternate"][0] == "error"
    assert "canonical" in results["alternate"][2].lower()
    assert not alternate_ready.is_set()

    current = store.resolve_current()
    assert current is not None
    final_ids = {
        row["messageId"]
        for path in (current / "canonical").glob("*.jsonl")
        for row in rich.load_jsonl(path)
    }
    successful_ids = {
        result[1] for result in results.values() if result[0] == "ok"
    }
    assert successful_ids <= final_ids
    assert alternate_message_id not in final_ids


def test_cross_process_public_publish_rejects_alternate_requested_lock(tmp_path):
    store = make_store(tmp_path)
    write_initial_generation(store, tmp_path, normalize())
    with incremental_run_context(tmp_path) as setup_context:
        canonical_stage = store.create_stage(
            "public-race-canonical",
            run_context=setup_context,
        )
        alternate_stage = store.create_stage(
            "public-race-alternate",
            run_context=setup_context,
        )

        def populate(stage, source):
            rows = rich.merge_day_records(
                rich.load_jsonl(stage / "canonical/2026-09-05.jsonl"),
                [normalize(source)],
            )
            rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", rows)
            rich._atomic_bytes(
                stage / "raw/2026-09-05.md",
                rich.render_day(rows).encode(),
            )
            manifest = rich.generation_inventory(stage)
            rich.atomic_json(stage / "generation-manifest.json", manifest)
            return manifest

        canonical_id = "1540000000000000002"
        alternate_id = "1540000000000000003"
        canonical_manifest = populate(
            canonical_stage,
            message(id=canonical_id, content="canonical public publish"),
        )
        alternate_manifest = populate(
            alternate_stage,
            message(id=alternate_id, content="alternate public publish"),
        )

    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    result_queue = context.Queue()
    canonical_worker = context.Process(
        target=_cross_process_publish_worker,
        args=(
            str(tmp_path),
            LOCK_NAME,
            "canonical",
            str(canonical_stage),
            "public-race-canonical",
            canonical_manifest["generationSha256"],
            ready,
            release,
            result_queue,
            True,
        ),
    )
    alternate_worker = context.Process(
        target=_cross_process_publish_worker,
        args=(
            str(tmp_path),
            "alternate.lock",
            "alternate",
            str(alternate_stage),
            "public-race-alternate",
            alternate_manifest["generationSha256"],
            context.Event(),
            release,
            result_queue,
            False,
        ),
    )
    canonical_worker.start()
    try:
        assert ready.wait(timeout=10)
        alternate_worker.start()
        alternate_worker.join(timeout=10)
        assert not alternate_worker.is_alive()
    finally:
        release.set()
        canonical_worker.join(timeout=10)
        if canonical_worker.is_alive():
            canonical_worker.terminate()
            canonical_worker.join(timeout=5)
        if alternate_worker.pid is not None and alternate_worker.is_alive():
            alternate_worker.terminate()
            alternate_worker.join(timeout=5)

    assert canonical_worker.exitcode == 0
    assert alternate_worker.exitcode == 0
    results = {}
    for _ in range(2):
        try:
            row = result_queue.get(timeout=5)
        except queue.Empty:
            pytest.fail("public publish worker omitted its result")
        results[row[0]] = row[1:]
    assert results["canonical"] == ("ok",)
    assert results["alternate"][0] == "error"
    assert "run context" in results["alternate"][2].lower()
    assert alternate_stage.is_dir()
    assert not (store.generations / "public-race-alternate").exists()

    current = store.resolve_current()
    assert current is not None and current.name == "public-race-canonical"
    final_ids = {
        row["messageId"]
        for path in (current / "canonical").glob("*.jsonl")
        for row in rich.load_jsonl(path)
    }
    assert canonical_id in final_ids
    assert alternate_id not in final_ids


def test_parallel_asset_reservation_failure_does_not_poison_shared_budget(tmp_path):
    limits = rich.AssetLimits(
        full_run_files=1,
        full_run_bytes=3,
        disk_reserve_bytes=0,
    )
    budget = rich.AssetRunBudget.from_limits(limits)
    asset = {
        "inScope": True,
        "declaredSize": 3,
        "localRelativePath": "attachments/1540000000000000001/asset.bin",
    }
    barrier = threading.Barrier(2)
    successes = []
    errors = []

    def reserve():
        try:
            barrier.wait(timeout=5)
            successes.append(budget.reserve_entry(
                [asset],
                tmp_path,
                assume_unknown_max=False,
            ))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=reserve) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], rich.AssetDownloadError)
    assert budget.file_count == 1
    assert budget.declared_bytes == 3


def test_publish_rejects_stage_when_current_changed_after_copy(tmp_path):
    store = make_store(tmp_path)
    write_initial_generation(store, tmp_path, normalize())
    with incremental_run_context(tmp_path) as run_context:
        lock_token = context_lock_token(run_context)
        first_stage = store.create_stage(
            "stale-cas-first",
            copy_current=True,
            lock_token=lock_token,
            run_context=run_context,
        )
        second_stage = store.create_stage(
            "stale-cas-second",
            copy_current=True,
            lock_token=lock_token,
            run_context=run_context,
        )
        stage_data = [
            (first_stage, normalize(message(id="1540000000000000002", content="first"))),
            (second_stage, normalize(message(id="1540000000000000003", content="second"))),
        ]
        manifests = []
        for stage, record in stage_data:
            rows = rich.merge_day_records(
                rich.load_jsonl(stage / "canonical/2026-09-05.jsonl"),
                [record],
            )
            rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", rows)
            rich._atomic_bytes(
                stage / "raw/2026-09-05.md", rich.render_day(rows).encode(),
            )
            manifest = rich.generation_inventory(stage)
            rich.atomic_json(stage / "generation-manifest.json", manifest)
            manifests.append(manifest)

        store.publish_stage(
            first_stage,
            "stale-cas-first",
            manifests[0]["generationSha256"],
            lock_token=lock_token,
            run_context=run_context,
        )
        with pytest.raises(rich.GenerationError, match="CURRENT.*changed|stale"):
            store.publish_stage(
                second_stage,
                "stale-cas-second",
                manifests[1]["generationSha256"],
                lock_token=lock_token,
                run_context=run_context,
            )

    current = store.resolve_current()
    assert current is not None and current.name == "stale-cas-first"
