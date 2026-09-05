#!/usr/bin/env python3
"""Run one bounded deterministic daily Discord rich-archive sync slot."""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


HERE = Path(__file__).resolve().parent
RICH_ARCHIVE_MODULE = HERE / "rich_message_archive.py"
ACTIVE_QUEUE_STATUSES = {"queued", "catching_up", "retry"}
INELIGIBLE_ENTRY_STATUSES = {"partial", "queued", "catching_up", "retry", "error", "excluded"}
MAX_429_RETRIES = 8
MAX_429_WAIT_SECONDS = 120.0
MAX_DISCORD_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DISCORD_ERROR_BYTES = 64 * 1024
SNOWFLAKE_RE = re.compile(r"^[0-9]{1,32}$")


class DailySyncError(RuntimeError):
    """A bounded operator-safe daily-sync failure."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def reject_symlink_components(path: Path, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise DailySyncError(f"unsafe_{label}")


def safe_path(value: str | Path, label: str, *, require_file: bool = False, require_directory: bool = False) -> Path:
    path = Path(os.path.abspath(Path(value).expanduser()))
    reject_symlink_components(path, label)
    if require_file and (not path.is_file() or path.is_symlink()):
        raise DailySyncError(f"missing_{label}")
    if require_directory and (not path.is_dir() or path.is_symlink()):
        raise DailySyncError(f"missing_{label}")
    return path


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DailySyncError(f"unreadable_{label}") from exc
    if not isinstance(value, dict):
        raise DailySyncError(f"invalid_{label}")
    return value


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    reject_symlink_components(path, "state")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise DailySyncError("unsafe_state_target")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise DailySyncError("state_persist_failed") from exc
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def snowflake_int(value: Any) -> int:
    text = str(value or "")
    return int(text) if SNOWFLAKE_RE.fullmatch(text) else 0


def newest_cursor(*values: Any) -> str | None:
    valid = [str(value) for value in values if snowflake_int(value) > 0]
    return max(valid, key=snowflake_int) if valid else None


def parse_day(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def inventory_reasons(payload: dict[str, Any], *, today: str, timezone_name: str) -> list[str]:
    reasons: list[str] = []
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise DailySyncError("invalid_timezone") from exc
    checked_at = payload.get("checkedAt")
    try:
        checked = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        if checked.tzinfo is None or checked.astimezone(zone).date().isoformat() != today:
            reasons.append("inventory_stale")
    except (TypeError, ValueError):
        reasons.append("inventory_stale")
    if payload.get("ok") is not True:
        reasons.append("inventory_not_ok")
    if not isinstance(payload.get("remainingMissing"), int) or payload.get("remainingMissing") != 0:
        reasons.append("inventory_incomplete")
    warnings = payload.get("warnings")
    if not isinstance(warnings, list) or warnings:
        reasons.append("inventory_warnings")
    return sorted(set(reasons))


def acquire_shared_lock(path: Path):
    reject_symlink_components(path, "lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise DailySyncError("unsafe_lock") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise DailySyncError("unsafe_lock")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise DailySyncError("unsafe_lock")
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def load_discord_token(config_path: Path, env_name: str) -> str:
    token = os.environ.get(env_name)
    if isinstance(token, str) and token.strip():
        return token
    config = load_json_object(config_path, "openclaw_config")
    channels = config.get("channels") if isinstance(config.get("channels"), dict) else {}
    discord = channels.get("discord") if isinstance(channels.get("discord"), dict) else {}
    token = discord.get("token")
    if not isinstance(token, str) or not token.strip():
        raise DailySyncError("discord_auth_unavailable")
    return token


def discord_messages(
    token: str,
    channel_id: str,
    *,
    after: str | None = None,
    around: str | None = None,
    limit: int,
    rate_limit_budget: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    cursor = after if after is not None else around
    if (
        not SNOWFLAKE_RE.fullmatch(channel_id)
        or not isinstance(cursor, str)
        or not SNOWFLAKE_RE.fullmatch(cursor)
        or (after is None) == (around is None)
    ):
        raise DailySyncError("invalid_discord_identity")
    query = {"limit": str(limit), "after" if after is not None else "around": cursor}
    params = urllib.parse.urlencode(query)
    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages?{params}",
        headers={"Authorization": f"Bot {token}", "User-Agent": "openclaw-discord-rich-daily/1.0"},
    )
    retries = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_bytes = int(declared)
                    except (TypeError, ValueError) as exc:
                        raise DailySyncError("discord_response_invalid") from exc
                    if declared_bytes < 0 or declared_bytes > MAX_DISCORD_RESPONSE_BYTES:
                        raise DailySyncError("discord_response_too_large")
                body = response.read(MAX_DISCORD_RESPONSE_BYTES + 1)
                if len(body) > MAX_DISCORD_RESPONSE_BYTES:
                    raise DailySyncError("discord_response_too_large")
                payload = json.loads(body.decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            body = exc.read(MAX_DISCORD_ERROR_BYTES + 1).decode("utf-8", errors="ignore")
            if exc.code != 429:
                raise DailySyncError("discord_fetch_failed") from exc
            retries += 1
            if retries > MAX_429_RETRIES:
                raise DailySyncError("discord_rate_limit_exhausted") from exc
            try:
                retry_after = float(json.loads(body).get("retry_after", 1.0))
            except (TypeError, ValueError, json.JSONDecodeError):
                retry_after = 1.0
            wait_seconds = min(max(retry_after, 0.0) + 0.25, 30.0)
            budget = rate_limit_budget if rate_limit_budget is not None else {"waited": 0.0}
            waited = float(budget.get("waited", 0.0))
            if waited + wait_seconds > MAX_429_WAIT_SECONDS:
                raise DailySyncError("discord_rate_limit_exhausted") from exc
            budget["waited"] = waited + wait_seconds
            time.sleep(wait_seconds)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DailySyncError("discord_fetch_failed") from exc
    if not isinstance(payload, list) or len(payload) > limit:
        raise DailySyncError("discord_response_invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    after_value = snowflake_int(after)
    for message in payload:
        if not isinstance(message, dict):
            raise DailySyncError("discord_response_invalid")
        message_id = str(message.get("id") or "")
        message_channel = str(message.get("channel_id") or channel_id)
        if (
            not SNOWFLAKE_RE.fullmatch(message_id)
            or (after is not None and snowflake_int(message_id) <= after_value)
            or message_id in seen
            or message_channel != channel_id
        ):
            raise DailySyncError("discord_response_invalid")
        seen.add(message_id)
        result.append(message)
    return sorted(result, key=lambda item: snowflake_int(item["id"]))


def entry_is_excluded(entry: dict[str, Any]) -> bool:
    return bool(entry.get("backupExcluded") or entry.get("invalidChannel") or entry.get("syncStatus") == "excluded")


def active_queue_keys(queue: dict[str, Any]) -> set[str]:
    items = queue.get("items")
    if not isinstance(items, list):
        raise DailySyncError("invalid_queue")
    keys: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise DailySyncError("invalid_queue")
        key = item.get("entryKey")
        if isinstance(key, str) and item.get("status", "queued") in ACTIVE_QUEUE_STATUSES:
            keys.add(key)
    return keys


def select_candidates(
    state: dict[str, Any], queue: dict[str, Any], *, today: str,
    max_entries: int, freshness_days: int,
) -> list[tuple[str, dict[str, Any]]]:
    entries = state.get("entries")
    if not isinstance(entries, dict):
        raise DailySyncError("invalid_state")
    try:
        current_day = date.fromisoformat(today)
    except ValueError as exc:
        raise DailySyncError("invalid_today") from exc
    stale_cutoff = current_day - timedelta(days=freshness_days)
    active = active_queue_keys(queue)
    candidates: list[tuple[str, dict[str, Any]]] = []
    for key, entry in entries.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise DailySyncError("invalid_state")
        if key in active or entry_is_excluded(entry) or entry.get("backlogReason"):
            continue
        status = str(entry.get("syncStatus") or "healthy")
        if status in INELIGIBLE_ENTRY_STATUSES or status != "healthy":
            continue
        cursor = newest_cursor(entry.get("lastWrittenMessageId"), entry.get("lastMessageId"))
        last_backup = parse_day(entry.get("lastBackup"))
        if cursor is None or last_backup is None or last_backup == current_day or last_backup <= stale_cutoff:
            continue
        channel_id = str(entry.get("channelId") or "")
        relative_path = entry.get("relativePath")
        if not SNOWFLAKE_RE.fullmatch(channel_id) or not isinstance(relative_path, str) or not relative_path.strip():
            continue
        candidates.append((key, entry))
    candidates.sort(key=lambda item: (str(item[1].get("lastBackup") or ""), str(item[1].get("relativePath") or ""), item[0]))
    return candidates[:max_entries]


def upsert_queue_item(queue: dict[str, Any], key: str, entry: dict[str, Any], *, status: str, reason: str) -> None:
    items = queue.setdefault("items", [])
    if not isinstance(items, list):
        raise DailySyncError("invalid_queue")
    cursor = newest_cursor(entry.get("lastWrittenMessageId"), entry.get("lastMessageId"))
    timestamp = now_utc()
    for item in items:
        if isinstance(item, dict) and item.get("entryKey") == key:
            item.update({
                "channelId": entry.get("channelId"),
                "relativePath": entry.get("relativePath", key),
                "type": entry.get("type"),
                "cursorMessageId": cursor,
                "status": status,
                "reason": reason,
                "updatedAt": timestamp,
            })
            if status in ACTIVE_QUEUE_STATUSES and item.get("attempts") is None:
                item["attempts"] = 0
            return
    items.append({
        "entryKey": key,
        "channelId": entry.get("channelId"),
        "relativePath": entry.get("relativePath", key),
        "type": entry.get("type"),
        "cursorMessageId": cursor,
        "priority": 50,
        "reason": reason,
        "status": status,
        "attempts": 0,
        "createdAt": timestamp,
        "updatedAt": timestamp,
    })


def safe_entry_root(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise DailySyncError("unsafe_entry_path")
    candidate = Path(os.path.abspath(root / relative))
    reject_symlink_components(candidate, "entry_path")
    if candidate != root and root not in candidate.parents:
        raise DailySyncError("unsafe_entry_path")
    return candidate


def load_rich_archive_module(path: Path = RICH_ARCHIVE_MODULE) -> ModuleType:
    path = safe_path(path, "rich_archive_module", require_file=True)
    spec = importlib.util.spec_from_file_location("managed_rich_message_archive", path)
    if spec is None or spec.loader is None:
        raise DailySyncError("rich_archive_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise DailySyncError("rich_archive_unavailable") from exc
    store = getattr(module, "RichArchiveStore", None)
    if not isinstance(store, type):
        raise DailySyncError("rich_archive_unavailable")
    return module


def verification_passed(result: dict[str, Any]) -> bool:
    if result.get("verified") is True or result.get("verificationOk") is True or result.get("ok") is True:
        return True
    verification = result.get("verification")
    return isinstance(verification, dict) and verification.get("ok") is True


def resolved_generation_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("generationId", "generation_id"):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        return None
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str) and value:
        return Path(value).name
    for attribute in ("generation_id", "generationId"):
        candidate = getattr(value, attribute, None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def merge_verified(
    store_class: type, *, entry_root: Path, lock_path: Path, downloader: Any,
    messages: list[dict[str, Any]], channel_id: str, observed_at: str,
    generation_id: str,
) -> tuple[str, dict[str, Any]]:
    try:
        store = store_class(entry_root, lock_path=lock_path)
        merge = getattr(store, "merge_messages", None)
        resolve = getattr(store, "resolve_current", None)
        if not callable(merge) or not callable(resolve):
            raise DailySyncError("rich_archive_contract_invalid")
        result = merge(
            messages,
            channel_id=channel_id,
            observed_at=observed_at,
            generation_id=generation_id,
            downloader=downloader,
            lock_already_held=True,
        )
        if not isinstance(result, dict):
            raise DailySyncError("rich_archive_verification_failed")
        committed = result.get("generationId")
        if not isinstance(committed, str) or not committed or not verification_passed(result):
            raise DailySyncError("rich_archive_verification_failed")
        current = resolved_generation_id(resolve())
        if current != committed:
            raise DailySyncError("rich_archive_readback_mismatch")
        return committed, result
    except DailySyncError:
        raise
    except Exception as exc:
        raise DailySyncError("rich_archive_merge_failed") from exc


def read_entry_messages(
    fetch: Callable[..., list[dict[str, Any]]], token: str, channel_id: str,
    cursor: str, *, page_limit: int, max_messages: int, remaining_read: int,
    lookback_limit: int, rate_limit_budget: dict[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int]:
    refresh: list[dict[str, Any]] = []
    lookback_budget = min(lookback_limit, remaining_read)
    if lookback_budget > 0:
        around = fetch(
            token, channel_id, around=cursor, limit=lookback_budget,
            rate_limit_budget=rate_limit_budget,
        )
        refresh = [message for message in around if snowflake_int(message.get("id")) <= snowflake_int(cursor)]

    messages: list[dict[str, Any]] = []
    after = cursor
    reads = len(around) if lookback_budget > 0 else 0
    complete = False
    for page_index in range(2):
        budget = min(page_limit, max_messages - len(messages), remaining_read - reads)
        if budget <= 0:
            break
        page = fetch(
            token, channel_id, after=after, limit=budget,
            rate_limit_budget=rate_limit_budget,
        )
        reads += len(page)
        if not page:
            complete = True
            break
        messages.extend(page)
        after = str(page[-1]["id"])
        if len(page) < budget:
            complete = True
            break
        if page_index == 0 and budget != page_limit:
            break
    by_id = {str(message["id"]): message for message in refresh}
    by_id.update({str(message["id"]): message for message in messages})
    merged = [by_id[key] for key in sorted(by_id, key=snowflake_int)]
    return merged, messages, complete, reads


def generation_id_for(role: str, channel_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"daily-{stamp}-{os.getpid()}-{role}-{channel_id}"


def validate_limits(args: argparse.Namespace) -> None:
    bounds = {
        "max_entries": (1, 6),
        "max_write_entries": (1, 4),
        "page_limit": (1, 30),
        "max_messages_per_entry": (1, 60),
        "max_read_messages": (1, 180),
        "lookback_limit": (1, 30),
        "freshness_days": (1, 7),
    }
    for name, (minimum, maximum) in bounds.items():
        value = getattr(args, name)
        if not isinstance(value, int) or not minimum <= value <= maximum:
            raise DailySyncError("invalid_limits")
    if (
        args.max_messages_per_entry < args.page_limit
        or args.max_read_messages < args.page_limit
        or args.max_read_messages < args.lookback_limit
    ):
        raise DailySyncError("invalid_limits")


def execute(args: argparse.Namespace, *, fetch: Callable[..., list[dict[str, Any]]] = discord_messages) -> tuple[dict[str, Any], int]:
    validate_limits(args)
    state_path = safe_path(args.state, "state", require_file=True)
    queue_path = safe_path(args.queue, "queue", require_file=True)
    inventory_path = safe_path(args.inventory, "inventory", require_file=True)
    root = safe_path(args.root, "root", require_directory=True)
    openclaw_config = safe_path(args.openclaw_config, "openclaw_config", require_file=True)
    lock_path = state_path.parent / ".channel_backup.lock"
    handle = acquire_shared_lock(lock_path)
    if handle is None:
        return {"ok": False, "status": "skipped", "reason": "backup_lock_busy"}, 0
    try:
        inventory = load_json_object(inventory_path, "inventory")
        reasons = inventory_reasons(inventory, today=args.today, timezone_name=args.timezone)
        if reasons:
            return {"ok": False, "status": "skipped", "reason": "inventory_blocked", "reasons": reasons}, 0

        state = load_json_object(state_path, "state")
        queue = load_json_object(queue_path, "queue")
        candidates = select_candidates(
            state, queue, today=args.today, max_entries=args.max_entries,
            freshness_days=args.freshness_days,
        )
        rich_module = load_rich_archive_module()
        store_class = rich_module.RichArchiveStore
        downloader_class = getattr(rich_module, "AssetDownloader", None)
        if not isinstance(downloader_class, type):
            raise DailySyncError("rich_archive_contract_invalid")
        downloader = downloader_class()
        token = load_discord_token(openclaw_config, args.token_env)
        rate_limit_budget: dict[str, float] = {"waited": 0.0}

        checked = 0
        written_entries = 0
        written_messages = 0
        refreshed_messages = 0
        merged_messages = 0
        queued = 0
        total_read = 0
        changed = False
        for key, entry in candidates:
            if written_entries >= args.max_write_entries or total_read >= args.max_read_messages:
                break
            checked += 1
            cursor = newest_cursor(entry.get("lastWrittenMessageId"), entry.get("lastMessageId"))
            channel_id = str(entry.get("channelId") or "")
            if cursor is None:
                raise DailySyncError("invalid_state_cursor")
            try:
                messages, new_messages, complete, read_count = read_entry_messages(
                    fetch, token, channel_id, cursor,
                    page_limit=args.page_limit,
                    max_messages=args.max_messages_per_entry,
                    remaining_read=args.max_read_messages - total_read,
                    lookback_limit=args.lookback_limit,
                    rate_limit_budget=rate_limit_budget,
                )
                total_read += read_count
                if not messages:
                    entry.update({
                        "syncStatus": "healthy", "backlogReason": None,
                        "lastBackup": args.today, "consecutiveErrors": 0,
                    })
                    changed = True
                    continue

                observed_at = now_utc()
                entry_root = safe_entry_root(root, str(entry["relativePath"]))
                committed_generation, _merge_result = merge_verified(
                    store_class,
                    entry_root=entry_root,
                    lock_path=lock_path,
                    downloader=downloader,
                    messages=messages,
                    channel_id=channel_id,
                    observed_at=observed_at,
                    generation_id=generation_id_for(args.role, channel_id),
                )
                committed_cursor = (
                    max((str(message["id"]) for message in new_messages), key=snowflake_int)
                    if new_messages else cursor
                )
                entry.update({
                    "lastWrittenMessageId": committed_cursor,
                    "lastMessageId": committed_cursor,
                    "lastSuccessfulWriteAt": observed_at,
                    "richArchiveIncrementalStatus": "verified",
                    "richArchiveIncrementalGenerationId": committed_generation,
                    "richArchiveIncrementalVerifiedAt": observed_at,
                    "consecutiveErrors": 0,
                })
                capped = not complete
                if capped:
                    entry.update({"syncStatus": "partial", "backlogReason": "page_limit_reached"})
                    upsert_queue_item(queue, key, entry, status="queued", reason="page_limit_reached")
                    queued += 1
                else:
                    entry.update({"syncStatus": "healthy", "backlogReason": None, "lastBackup": args.today})
                written_entries += 1
                written_messages += len(new_messages)
                refreshed_messages += len(messages) - len(new_messages)
                merged_messages += len(messages)
                changed = True
            except DailySyncError as exc:
                entry["syncStatus"] = "error"
                entry["backlogReason"] = "read_error"
                entry["consecutiveErrors"] = int(entry.get("consecutiveErrors") or 0) + 1
                upsert_queue_item(queue, key, entry, status="retry", reason="read_error")
                state["updatedAt"] = now_utc()
                queue["updatedAt"] = now_utc()
                # Queue-first is the safer side of a two-file commit: if state
                # persistence then fails, a durable retry still owns the old
                # cursor instead of leaving a partial entry stranded.
                atomic_json(queue_path, queue)
                atomic_json(state_path, state)
                raise exc

        if changed:
            state["updatedAt"] = now_utc()
            queue["updatedAt"] = now_utc()
            # Archive CURRENT is already published. Persist ownership/queue
            # first, then advance state; an interruption can only cause an
            # idempotent replay, never a cursor-ahead-of-archive condition.
            atomic_json(queue_path, queue)
            atomic_json(state_path, state)
        active_left = sum(
            1 for item in queue.get("items", [])
            if isinstance(item, dict) and item.get("status") in ACTIVE_QUEUE_STATUSES
        )
        status = "pending" if queued else "ok"
        return {
            "ok": True,
            "status": status,
            "checked": checked,
            "writtenEntries": written_entries,
            "writtenMessages": written_messages,
            "refreshedMessages": refreshed_messages,
            "mergedMessages": merged_messages,
            "queued": queued,
            "totalRead": total_read,
            "activeQueueLeft": active_left,
        }, 0
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one deterministic daily Discord rich-archive sync slot.")
    parser.add_argument("--role", required=True, choices=("daily-sync-1", "daily-sync-2", "daily-sync-3"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--today", required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--openclaw-config", required=True)
    parser.add_argument("--token-env", default="DISCORD_BOT_TOKEN")
    parser.add_argument("--max-entries", type=int, default=6)
    parser.add_argument("--max-write-entries", type=int, default=4)
    parser.add_argument("--page-limit", type=int, default=30)
    parser.add_argument("--max-messages-per-entry", type=int, default=60)
    parser.add_argument("--max-read-messages", type=int, default=180)
    parser.add_argument("--lookback-limit", type=int, default=10)
    parser.add_argument("--freshness-days", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    try:
        result, exit_code = execute(parse_args())
    except DailySyncError as exc:
        result, exit_code = {"ok": False, "status": "error", "reason": exc.category}, 2
    except Exception:
        result, exit_code = {"ok": False, "status": "error", "reason": "unexpected_error"}, 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
