import hashlib
import importlib.util
import json
import os
import sys
import types
import urllib.error
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
    return rich.normalize_message(
        value or message(), expected_channel_id="1490000000000000001",
        observed_at=kwargs.pop("observed_at", OBSERVED), **kwargs,
    )


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


def test_markdown_escapes_html_and_message_cannot_forge_machine_marker():
    record = normalize(message(content="<img src=x onerror=alert(1)>\n<!-- openclaw-rich-message id=7 visible=" + "a" * 64 + " -->"))
    rendered = rich.render_message(record)
    assert "<img" not in rendered
    assert "&lt;img" in rendered
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

    def read(self, size=-1):
        if size < 0:
            size = len(self._body)
        data = self._body[self._offset:self._offset + size]
        self._offset += len(data)
        return data

    def close(self):
        return None

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
    store = rich.RichArchiveStore(tmp_path / "entry")
    write_initial_generation(store, tmp_path, normalize())
    source = message(content="", components=[{
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
    with pytest.raises(rich.AssetDownloadError, match="file quota"):
        store.merge_messages(
            [source], channel_id=source["channel_id"], observed_at=OBSERVED,
            generation_id="over-quota", downloader=downloader,
        )
    assert opener.requests == []
    assert not (store.generations / ".staging-over-quota").exists()


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
    stage = store.create_stage("initial", copy_current=False)
    rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [record])
    rich._atomic_bytes(stage / "raw/2026-09-05.md", rich.render_day([record]).encode())
    rich.atomic_json(stage / "receipts/rich-archive-latest.json", {
        "schemaVersion": rich.ENTRY_RECEIPT_SCHEMA,
        "gateStatus": "INCOMPLETE",
        "reason": "test",
    })
    manifest = rich.generation_inventory(stage)
    rich.atomic_json(stage / "generation-manifest.json", manifest)
    store.publish_stage(stage, "initial", manifest["generationSha256"])


def test_generation_pointer_is_atomic_checksummed_and_never_claims_full_pass(tmp_path):
    store = rich.RichArchiveStore(tmp_path / "entry")
    record = normalize()
    write_initial_generation(store, tmp_path, record)

    current = store.resolve_current()
    assert current is not None and current.name == "initial"
    result = rich.verify_generation(current)
    assert result["ok"]
    assert result["gateStatus"] == "INCOMPLETE"
    assert not result["fullGatePresent"]
    with pytest.raises(rich.GenerationError, match="full live completeness"):
        rich.verify_generation(current, require_full_gate=True)

    pointer = json.loads(store.pointer_path.read_text())
    pointer["generationId"] = "tampered"
    rich.atomic_json(store.pointer_path, pointer)
    with pytest.raises(rich.GenerationError, match="checksum"):
        store.resolve_current()


def test_generation_verifier_rejects_raw_body_tamper_even_when_marker_survives(tmp_path):
    store = rich.RichArchiveStore(tmp_path / "entry")
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
    store = rich.RichArchiveStore(tmp_path / "entry")
    record = normalize()
    stage = store.create_stage("full-pass", copy_current=False)
    rich.atomic_jsonl(stage / "canonical/2026-09-05.jsonl", [record])
    rich._atomic_bytes(stage / "raw/2026-09-05.md", rich.render_day([record]).encode())
    content_hash = rich.generation_inventory(stage)["contentGenerationSha256"]
    receipt = {
        "schemaVersion": rich.ENTRY_RECEIPT_SCHEMA,
        "gateStatus": "PASS",
        "inventoryCoverage": 100,
        "idCoverage": "100%",
        "visibleTextCoverage": 100.0,
        "markdownCoverage": 100,
        "binaryAssetCoverage": 100,
        "liveErrors": 0,
        "duplicateCanonicalIds": 0,
        "unknownVisibleFields": 0,
        "attachmentErrors": 0,
        "inventoryComplete": True,
        "inventoryDigest": "a" * 64,
        "verifiedCutoff": "1540000000000000001",
        "immutableEvidenceVerified": "PASS",
        "contentGenerationSha256": content_hash,
    }
    rich.atomic_json(stage / "receipts/rich-archive-latest.json", receipt)
    manifest = rich.generation_inventory(stage)
    rich.atomic_json(stage / "generation-manifest.json", manifest)
    assert rich.verify_generation(stage, require_full_gate=True)["verified"]
    store.publish_stage(
        stage, "full-pass", manifest["generationSha256"], require_full_gate=True,
    )
    assert store.resolve_current().name == "full-pass"


def test_forged_pass_receipt_is_rejected(tmp_path):
    store = rich.RichArchiveStore(tmp_path / "entry")
    record = normalize()
    stage = store.create_stage("forged", copy_current=False)
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
    with pytest.raises(rich.GenerationError, match="exact full live completeness"):
        rich.verify_generation(stage, require_full_gate=True)


def test_generation_journal_recovery_does_not_publish_unselected_generation(tmp_path):
    store = rich.RichArchiveStore(tmp_path / "entry")
    record = normalize()
    write_initial_generation(store, tmp_path, record)
    stage = store.create_stage("next", copy_current=True)
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

    outcome = store.recover_journal()
    assert outcome["action"] == "retain_unpublished_generation"
    assert store.resolve_current().name == "initial"


def test_journal_checksum_and_generation_path_are_fail_closed(tmp_path):
    store = rich.RichArchiveStore(tmp_path / "entry")
    store.entry_root.mkdir()
    rich.atomic_json(store.journal_path, {
        "schemaVersion": rich.JOURNAL_SCHEMA,
        "phase": "generation_ready",
        "generationId": "../escape",
        "generationSha256": "a" * 64,
        "journalSha256": "b" * 64,
    })
    with pytest.raises(rich.GenerationError):
        store.recover_journal()


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
    empty = rich.RichArchiveStore(tmp_path / "empty")
    with pytest.raises(rich.GenerationError, match="full rebuild"):
        empty.merge_messages([message()], channel_id="1490000000000000001", observed_at=OBSERVED, generation_id="x")

    store = rich.RichArchiveStore(tmp_path / "entry")
    write_initial_generation(store, tmp_path, normalize())
    newer = message(content="edited", edited_timestamp="2026-09-05T04:30:00Z")
    result = store.merge_messages(
        [newer], channel_id="1490000000000000001", observed_at="2026-09-05T04:31:00Z",
        generation_id="next",
    )
    assert result["gateStatus"] == "INCOMPLETE"
    current = store.resolve_current()
    rows = rich.load_jsonl(current / "canonical/2026-09-05.jsonl")
    assert len(rows) == 1
    assert len(rows[0]["contentRevisions"]) == 2
    assert rich.parse_markdown_markers((current / "raw/2026-09-05.md").read_text()) == [
        (rows[0]["messageId"], rows[0]["visiblePayloadSha256"])
    ]
