from __future__ import annotations

import argparse
import io
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill/openclaw-discord-server-backup"
SCRIPT = SKILL / "scripts/run_daily_sync_v3.py"
spec = importlib.util.spec_from_file_location("run_daily_sync_v3", SCRIPT)
daily = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = daily
spec.loader.exec_module(daily)


def setup_run(tmp_path: Path) -> tuple[argparse.Namespace, Path, Path, Path]:
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    archive = tmp_path / "customer" / "Discord資料"
    entry_root = archive / "Entry"
    memory.mkdir(parents=True)
    entry_root.mkdir(parents=True)
    state = memory / "state.json"
    queue = memory / "queue.json"
    inventory = memory / "inventory.json"
    config = memory / "openclaw.json"
    state.write_text(json.dumps({
        "entries": {
            "entry": {
                "channelId": "1490000000000000001",
                "relativePath": "Entry",
                "type": "channel",
                "syncStatus": "healthy",
                "lastBackup": "2026-09-04",
                "lastWrittenMessageId": "1540000000000000001",
                "lastMessageId": "1540000000000000001",
            }
        }
    }), encoding="utf-8")
    queue.write_text('{"version":1,"items":[]}', encoding="utf-8")
    inventory.write_text(json.dumps({
        "ok": True,
        "checkedAt": "2026-09-05T05:25:00+08:00",
        "remainingMissing": 0,
        "warnings": [],
    }), encoding="utf-8")
    config.write_text(json.dumps({"channels": {"discord": {"token": "test-token"}}}), encoding="utf-8")
    args = argparse.Namespace(
        role="daily-sync-1",
        state=str(state),
        queue=str(queue),
        root=str(archive),
        inventory=str(inventory),
        today="2026-09-05",
        timezone="Asia/Taipei",
        openclaw_config=str(config),
        token_env="TEST_DISCORD_TOKEN_THAT_IS_NOT_SET",
        max_entries=6,
        max_write_entries=4,
        page_limit=30,
        max_messages_per_entry=60,
        max_read_messages=180,
        lookback_limit=10,
        freshness_days=2,
    )
    return args, state, queue, entry_root


def api_message(message_id: str, *, content: str = "hello") -> dict:
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


def fake_rich_module(*, fail_merge: bool = False):
    instances = []

    class Downloader:
        pass

    class Store:
        def __init__(self, entry_root: Path, *, lock_path: Path | None = None):
            self.entry_root = entry_root
            self.lock_path = lock_path
            self.current: Path | None = None
            self.merged = []
            instances.append(self)

        def merge_messages(self, messages, **kwargs):
            if fail_merge:
                raise RuntimeError("injected merge failure")
            assert kwargs["downloader"].__class__ is Downloader
            assert kwargs["lock_already_held"] is True
            self.merged = list(messages)
            generation = kwargs["generation_id"]
            self.current = self.entry_root / "generations" / generation
            return {"generationId": generation, "verified": True, "ok": True}

        def resolve_current(self):
            return self.current

    return types.SimpleNamespace(RichArchiveStore=Store, AssetDownloader=Downloader, instances=instances)


def test_lookback_refreshes_existing_message_without_advancing_cursor(monkeypatch, tmp_path):
    args, state_path, _queue_path, _entry_root = setup_run(tmp_path)
    rich = fake_rich_module()
    monkeypatch.setattr(daily, "load_rich_archive_module", lambda: rich)
    calls = []

    def fetch(_token, _channel, *, after=None, around=None, limit, rate_limit_budget):
        calls.append((after, around, limit, rate_limit_budget))
        if around:
            return [api_message("1540000000000000001", content="edited")]
        return []

    result, code = daily.execute(args, fetch=fetch)
    assert code == 0 and result["ok"] is True
    assert result["writtenMessages"] == 0
    assert result["refreshedMessages"] == 1
    assert result["mergedMessages"] == 1
    assert calls[0][1] == "1540000000000000001"
    assert calls[1][0] == "1540000000000000001"
    entry = json.loads(state_path.read_text())["entries"]["entry"]
    assert entry["lastWrittenMessageId"] == "1540000000000000001"
    assert entry["richArchiveIncrementalStatus"] == "verified"
    assert "richArchiveStatus" not in entry
    assert rich.instances[0].merged[0]["content"] == "edited"


def test_new_cursor_moves_only_after_verified_publish(monkeypatch, tmp_path):
    args, state_path, _queue_path, _entry_root = setup_run(tmp_path)
    rich = fake_rich_module()
    monkeypatch.setattr(daily, "load_rich_archive_module", lambda: rich)

    def fetch(_token, _channel, *, after=None, around=None, limit, rate_limit_budget):
        if around:
            return [api_message("1540000000000000001")]
        return [api_message("1540000000000000002", content="new")]

    result, code = daily.execute(args, fetch=fetch)
    assert code == 0 and result["writtenMessages"] == 1
    entry = json.loads(state_path.read_text())["entries"]["entry"]
    assert entry["lastWrittenMessageId"] == "1540000000000000002"
    assert entry["lastMessageId"] == "1540000000000000002"


def test_merge_failure_keeps_cursor_and_queues_retry(monkeypatch, tmp_path):
    args, state_path, queue_path, _entry_root = setup_run(tmp_path)
    monkeypatch.setattr(daily, "load_rich_archive_module", lambda: fake_rich_module(fail_merge=True))

    def fetch(_token, _channel, *, after=None, around=None, limit, rate_limit_budget):
        return [api_message("1540000000000000001")] if around else [api_message("1540000000000000002")]

    with pytest.raises(daily.DailySyncError, match="rich_archive_merge_failed"):
        daily.execute(args, fetch=fetch)
    entry = json.loads(state_path.read_text())["entries"]["entry"]
    assert entry["lastWrittenMessageId"] == "1540000000000000001"
    assert entry["syncStatus"] == "error"
    items = json.loads(queue_path.read_text())["items"]
    assert items[0]["status"] == "retry"


def test_busy_shared_lock_is_safe_skip_before_discord_read(monkeypatch, tmp_path):
    args, _state_path, _queue_path, _entry_root = setup_run(tmp_path)
    handle = daily.acquire_shared_lock(Path(args.state).parent / ".channel_backup.lock")
    assert handle is not None
    called = False

    def fetch(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    try:
        result, code = daily.execute(args, fetch=fetch)
    finally:
        handle.close()
    assert code == 0
    assert result == {"ok": False, "status": "skipped", "reason": "backup_lock_busy"}
    assert called is False


def test_shared_lock_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "lock"
    link.symlink_to(target)
    with pytest.raises(daily.DailySyncError, match="unsafe_lock"):
        daily.acquire_shared_lock(link)


def test_discord_response_body_cap_is_fail_closed(monkeypatch):
    class Response:
        headers = {"Content-Length": str(daily.MAX_DISCORD_RESPONSE_BYTES + 1)}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(daily.urllib.request, "urlopen", lambda *_a, **_kw: Response())
    with pytest.raises(daily.DailySyncError, match="discord_response_too_large"):
        daily.discord_messages(
            "redacted", "1490000000000000001", after="1540000000000000001", limit=30,
            rate_limit_budget={"waited": 0.0},
        )


def test_discord_aggregate_rate_limit_wait_is_capped(monkeypatch):
    def limited(*_args, **_kwargs):
        raise daily.urllib.error.HTTPError(
            "https://discord.invalid", 429, "rate limited", {},
            io.BytesIO(b'{"retry_after":1.0}'),
        )

    monkeypatch.setattr(daily.urllib.request, "urlopen", limited)
    monkeypatch.setattr(daily.time, "sleep", lambda _seconds: None)
    budget = {"waited": daily.MAX_429_WAIT_SECONDS - 0.1}
    with pytest.raises(daily.DailySyncError, match="discord_rate_limit_exhausted"):
        daily.discord_messages(
            "redacted", "1490000000000000001", after="1540000000000000001", limit=30,
            rate_limit_budget=budget,
        )
    assert budget["waited"] == daily.MAX_429_WAIT_SECONDS - 0.1


def test_real_rich_store_keyword_contract_and_readback(tmp_path):
    rich_path = SKILL / "scripts/rich_message_archive.py"
    assert rich_path.is_file(), "rich archive core must ship with the deterministic daily runner"
    rich_spec = importlib.util.spec_from_file_location("daily_runner_real_rich", rich_path)
    rich = importlib.util.module_from_spec(rich_spec)
    assert rich_spec and rich_spec.loader
    sys.modules[rich_spec.name] = rich
    rich_spec.loader.exec_module(rich)

    entry_root = tmp_path / "Entry"
    lock_path = tmp_path / ".channel_backup.lock"
    store = rich.RichArchiveStore(entry_root, lock_path=lock_path)
    stage = store.create_stage("base", copy_current=False)
    rich.atomic_json(stage / "receipts/rich-archive-latest.json", {
        "schemaVersion": rich.ENTRY_RECEIPT_SCHEMA,
        "gateStatus": "INCOMPLETE",
        "reason": "test_base",
        "messageCount": 0,
        "unknownVisibleFields": 0,
        "attachmentErrors": 0,
    })
    manifest = rich.generation_inventory(stage)
    rich.atomic_json(stage / "generation-manifest.json", manifest)
    store.publish_stage(stage, "base", manifest["generationSha256"])

    generation, result = daily.merge_verified(
        rich.RichArchiveStore,
        entry_root=entry_root,
        lock_path=lock_path,
        downloader=rich.AssetDownloader(),
        messages=[api_message("1540000000000000002", content="rich content")],
        channel_id="1490000000000000001",
        observed_at="2026-09-05T04:00:00+00:00",
        generation_id="daily-integration",
    )
    assert generation == "daily-integration"
    assert result["verified"] is True and result["ok"] is True
    current = rich.RichArchiveStore(entry_root, lock_path=lock_path).resolve_current()
    assert current is not None and current.name == generation
    rendered = (current / "raw/2026-09-05.md").read_text(encoding="utf-8")
    assert "rich content" in rendered
