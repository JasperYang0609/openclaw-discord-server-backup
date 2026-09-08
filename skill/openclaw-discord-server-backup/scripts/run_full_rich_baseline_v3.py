#!/usr/bin/env python3
"""Root-atomic full rich baseline builder for the managed Discord archive.

The runner performs one live, bounded Discord inventory and message scan under
the canonical archive lock.  Every entry generation is sealed without changing
its compatibility CURRENT pointer.  Only after all generations verify does the
runner atomically publish RUN_CURRENT.json, read it back, and then publish the
per-entry CURRENT pointers used by the incremental runner.

The state and backlog queue are read-only inputs.  Their byte digests must be
unchanged at completion, so enrichment can never advance a cursor.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import audit_discord_inventory_v3 as inventory_api  # noqa: E402
import rich_message_archive as rich  # noqa: E402


RUNNER_SCHEMA = "openclaw-discord-rich-baseline-runner.v3"
RUN_MANIFEST_SCHEMA = "openclaw-discord-rich-root-run-manifest.v1"
RUN_POINTER_SCHEMA = "openclaw-discord-rich-root-current.v1"
RUN_RECEIPT_SCHEMA = "openclaw-discord-rich-baseline-receipt.v1"
ERROR_SCHEMA = "openclaw-discord-rich-baseline-error.v1"
SNOWFLAKE_RE = re.compile(r"^[0-9]{1,32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_429_RETRIES = 10


class BaselineError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_nlink),
        int(stat.S_IMODE(info.st_mode)),
    )


def require_identity(path: Path, expected: tuple[int, int, int, int]) -> None:
    try:
        actual = path_identity(path)
    except OSError as exc:
        raise BaselineError("managed_path_identity_changed") from exc
    if actual != expected:
        raise BaselineError("managed_path_identity_changed")


def directory_identity(path: Path) -> tuple[int, int, int]:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise BaselineError("managed_directory_invalid")
    return (int(info.st_dev), int(info.st_ino), int(stat.S_IMODE(info.st_mode)))


def require_directory_identity(path: Path, expected: tuple[int, int, int]) -> None:
    try:
        actual = directory_identity(path)
    except OSError as exc:
        raise BaselineError("managed_path_identity_changed") from exc
    if actual != expected:
        raise BaselineError("managed_path_identity_changed")


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise BaselineError("managed_json_unreadable") from exc
    if not isinstance(value, dict):
        raise BaselineError("managed_json_invalid")
    return value


def safe_absolute(value: str, *, file: bool = False, directory: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise BaselineError("managed_path_not_absolute")
    resolved = path.resolve(strict=True)
    if resolved != path or path.is_symlink():
        raise BaselineError("managed_path_identity_invalid")
    info = path.lstat()
    if file and info.st_nlink != 1:
        raise BaselineError("managed_path_link_count_invalid")
    if file and not stat.S_ISREG(info.st_mode):
        raise BaselineError("managed_file_invalid")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise BaselineError("managed_directory_invalid")
    if directory and stat.S_IMODE(info.st_mode) & 0o077:
        raise BaselineError("managed_directory_mode_invalid")
    return path


def safe_entry_root(archive_root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if (
        not relative_path
        or pure.is_absolute()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise BaselineError("entry_path_invalid")
    candidate = archive_root.joinpath(*pure.parts)
    current = archive_root
    for part in pure.parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise BaselineError("entry_path_symlink")
    if candidate == archive_root or archive_root not in candidate.parents:
        raise BaselineError("entry_path_escape")
    return candidate


class DiscordTransport:
    def __init__(self, token: str, *, max_requests: int, deadline_monotonic: float) -> None:
        if not isinstance(token, str) or not token.strip():
            raise BaselineError("discord_auth_unavailable")
        self._token = token.strip()
        self._max_requests = max_requests
        self._deadline_monotonic = deadline_monotonic
        self.requests = 0
        self.retries = 0
        self.waited_seconds = 0.0

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(
            f"https://discord.com/api/v10{path}{query}",
            headers={
                "Authorization": f"Bot {self._token}",
                "User-Agent": "openclaw-discord-rich-baseline/3.0",
            },
        )
        retry_count = 0
        while True:
            if time.monotonic() > self._deadline_monotonic:
                raise BaselineError("runtime_budget_exhausted")
            if self.requests >= self._max_requests:
                raise BaselineError("request_budget_exhausted")
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    encoded = response.read(MAX_RESPONSE_BYTES + 1)
                if len(encoded) > MAX_RESPONSE_BYTES:
                    raise BaselineError("discord_response_too_large")
                return json.loads(encoded.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read(MAX_ERROR_BYTES + 1)
                if exc.code != 429:
                    raise BaselineError("discord_fetch_failed") from exc
                retry_count += 1
                self.retries += 1
                if retry_count > MAX_429_RETRIES:
                    raise BaselineError("discord_rate_limit_exhausted") from exc
                try:
                    delay = float(
                        json.loads(body.decode("utf-8", errors="ignore")).get(
                            "retry_after", 1.0
                        )
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    delay = 1.0
                delay = min(max(delay, 0.0) + 0.25, 30.0)
                self.waited_seconds += delay
                time.sleep(delay)
            except (OSError, ValueError, TypeError) as exc:
                raise BaselineError("discord_fetch_failed") from exc

    def page(
        self,
        channel_id: str,
        *,
        before: str | None,
        limit: int,
    ) -> Mapping[str, Any]:
        if not SNOWFLAKE_RE.fullmatch(channel_id) or not 1 <= limit <= 100:
            raise BaselineError("discord_request_invalid")
        params = {"limit": str(limit)}
        if before is not None:
            if not SNOWFLAKE_RE.fullmatch(before):
                raise BaselineError("discord_request_invalid")
            params["before"] = before
        payload = self.get(f"/channels/{channel_id}/messages", params)
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise BaselineError("discord_response_invalid")
        return {
            "schemaVersion": rich.DISCORD_PAGE_RESPONSE_SCHEMA,
            "source": "discord-api-runtime-page",
            "request": {
                "channelId": channel_id,
                "before": before,
                "limit": limit,
            },
            "complete": True,
            "truncated": False,
            "responseCount": len(payload),
            "messages": payload,
        }


def load_token(config_path: Path, env_name: str) -> str:
    token = os.environ.get(env_name)
    if isinstance(token, str) and token.strip():
        return token.strip()
    config = load_object(config_path)
    discord = ((config.get("channels") or {}).get("discord") or {})
    token = discord.get("token") if isinstance(discord, Mapping) else None
    if not isinstance(token, str) or not token.strip():
        raise BaselineError("discord_auth_unavailable")
    return token.strip()


def state_entries(state: Mapping[str, Any], archive_root: Path) -> list[dict[str, Any]]:
    values = state.get("entries")
    if not isinstance(values, Mapping) or not values:
        raise BaselineError("state_entries_invalid")
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    paths: set[str] = set()
    for key, raw in values.items():
        if not isinstance(key, str) or not isinstance(raw, Mapping):
            raise BaselineError("state_entries_invalid")
        channel_id = str(raw.get("channelId") or "")
        relative_path = str(raw.get("relativePath") or "")
        entry_type = str(raw.get("type") or "")
        normalized = unicodedata.normalize("NFKC", relative_path).casefold()
        if (
            not SNOWFLAKE_RE.fullmatch(channel_id)
            or entry_type not in {"channel", "thread"}
            or channel_id in ids
            or normalized in paths
        ):
            raise BaselineError("state_entry_identity_invalid")
        safe_entry_root(archive_root, relative_path)
        ids.add(channel_id)
        paths.add(normalized)
        rows.append({
            "channelId": channel_id,
            "relativePath": relative_path,
            "normalizedRelativePath": normalized,
            "type": entry_type,
            "parentChannelId": None,
        })
    return sorted(rows, key=lambda row: int(row["channelId"]))


def live_inventory(
    transport: DiscordTransport,
    guild_id: str,
    expected: Sequence[Mapping[str, Any]],
    *,
    archived_page_limit: int,
) -> dict[str, Any]:
    channels, threads, warnings, metrics = inventory_api.collect_inventory(
        transport,
        guild_id,
        archived_page_limit=archived_page_limit,
    )
    if warnings or metrics.get("archivedEnumerationStatus") != "complete":
        raise BaselineError("discord_inventory_incomplete")
    live_ids = {
        str(row.get("id") or ""): "channel" for row in channels
    } | {
        str(row.get("id") or ""): "thread" for row in threads
    }
    expected_ids = {str(row["channelId"]): str(row["type"]) for row in expected}
    if live_ids != expected_ids:
        raise BaselineError("discord_inventory_identity_mismatch")
    return {
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
        "pageCount": max(1, int(metrics.get("archivedThreadsObserved") or 0) + 2),
        "responseCount": len(expected),
        "entries": [
            {
                "channelId": str(row["channelId"]),
                "relativePath": str(row["relativePath"]),
                "normalizedRelativePath": str(row["normalizedRelativePath"]),
            }
            for row in expected
        ],
    }


def archive_tree_manifest(archive_root: Path) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    for path in sorted(archive_root.rglob("*"), key=lambda item: str(item.relative_to(archive_root))):
        relative = str(path.relative_to(archive_root))
        if relative == ".channel_backup.lock" or relative.startswith("runs/"):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise BaselineError("archive_tree_symlink")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise BaselineError("archive_tree_hardlink")
            rows.append({
                "path": relative,
                "size": info.st_size,
                "sha256": file_sha256(path),
            })
    return rows, json_sha256(rows)


def immutable_reference(
    archive_root: Path,
    state_path: Path,
    queue_path: Path,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows, archive_digest = archive_tree_manifest(archive_root)
    state_digest = file_sha256(state_path)
    queue_digest = file_sha256(queue_path)
    verified_at = datetime.now(timezone.utc).isoformat()
    verification = {
        "archiveTreeManifestSha256": archive_digest,
        "stateSha256": state_digest,
        "queueSha256": queue_digest,
        "entryCount": len(rows),
        "verifiedAt": verified_at,
    }
    verification_digest = json_sha256(verification)
    reference = {
        "snapshotId": f"pre-rich-{run_id}",
        "status": "PASS",
        "verifiedAt": verified_at,
        "archiveTreeManifestSha256": archive_digest,
        "stateSha256": state_digest,
        "queueSha256": queue_digest,
        "verificationSha256": verification_digest,
    }
    evidence = {
        "schemaVersion": "openclaw-discord-immutable-evidence.v1",
        **verification,
        "verificationSha256": verification_digest,
        "files": rows,
    }
    return reference, evidence


def pointer_body(run_id: str, manifest_sha256: str) -> dict[str, Any]:
    body = {
        "schemaVersion": RUN_POINTER_SCHEMA,
        "runId": run_id,
        "runManifestSha256": manifest_sha256,
    }
    body["pointerSha256"] = json_sha256(body)
    return body


def verify_root_pointer(archive_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pointer_path = archive_root / "RUN_CURRENT.json"
    pointer = load_object(pointer_path)
    check = dict(pointer)
    supplied = check.pop("pointerSha256", None)
    if (
        pointer.get("schemaVersion") != RUN_POINTER_SCHEMA
        or not isinstance(pointer.get("runId"), str)
        or supplied != json_sha256(check)
    ):
        raise BaselineError("root_pointer_invalid")
    manifest_path = rich.contained_path(
        archive_root,
        f"runs/{pointer['runId']}/run-manifest.json",
    )
    if file_sha256(manifest_path) != pointer.get("runManifestSha256"):
        raise BaselineError("root_manifest_checksum_mismatch")
    manifest = load_object(manifest_path)
    if manifest.get("schemaVersion") != RUN_MANIFEST_SCHEMA:
        raise BaselineError("root_manifest_invalid")
    return pointer, manifest


def publish_compatibility(
    archive_root: Path,
    entries: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    run_context: rich.FullRebuildRunContext,
) -> int:
    selected = manifest.get("entries")
    if not isinstance(selected, list) or len(selected) != len(entries):
        raise BaselineError("root_manifest_entry_count_mismatch")
    by_id = {str(row.get("channelId") or ""): row for row in selected if isinstance(row, Mapping)}
    updated = 0
    for entry in entries:
        channel_id = str(entry["channelId"])
        chosen = by_id.get(channel_id)
        if not isinstance(chosen, Mapping):
            raise BaselineError("root_manifest_entry_missing")
        if chosen.get("relativePath") != entry["relativePath"]:
            raise BaselineError("root_manifest_path_mismatch")
        generation_id = str(chosen.get("generationId") or "")
        generation_sha256 = str(chosen.get("generationSha256") or "")
        if not generation_id or not HASH_RE.fullmatch(generation_sha256):
            raise BaselineError("root_manifest_generation_invalid")
        store = rich.RichArchiveStore(
            safe_entry_root(archive_root, str(entry["relativePath"])),
            lock_path=rich.canonical_archive_lock_path(archive_root),
        )
        current = store.resolve_current()
        needs_update = current is None or current.name != generation_id
        store.publish_existing_generation_pointer(
            generation_id,
            generation_sha256,
            run_context=run_context,
        )
        if needs_update:
            updated += 1
    return updated


def remaining_evidence_ttl(run_deadline_monotonic: float) -> float:
    """Bind local materialization authority to the remaining run deadline."""
    remaining = run_deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise BaselineError("runtime_budget_exhausted")
    return min(remaining, rich.MAX_LIVE_EVIDENCE_TTL_SECONDS)


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,126}[A-Za-z0-9]", args.run_id)
        or not SNOWFLAKE_RE.fullmatch(args.guild_id)
        or not 1 <= args.page_size <= 100
        or args.archived_page_limit < 1
        or args.max_pages_per_entry < 1
        or args.max_messages_per_entry < 0
        or args.max_requests < 1
        or args.max_runtime_seconds < 1
        or args.max_runtime_seconds > rich.MAX_LIVE_EVIDENCE_TTL_SECONDS
    ):
        raise BaselineError("runtime_arguments_invalid")
    archive_root = safe_absolute(args.root, directory=True)
    state_path = safe_absolute(args.state, file=True)
    queue_path = safe_absolute(args.queue, file=True)
    openclaw_config = safe_absolute(args.openclaw_config, file=True)
    state = load_object(state_path)
    if str(state.get("guildId") or "") != args.guild_id:
        raise BaselineError("guild_identity_mismatch")
    entries = state_entries(state, archive_root)
    initial_state_sha256 = file_sha256(state_path)
    initial_queue_sha256 = file_sha256(queue_path)
    archive_identity = directory_identity(archive_root)
    state_identity = path_identity(state_path)
    queue_identity = path_identity(queue_path)
    token = load_token(openclaw_config, args.token_env)
    run_deadline_monotonic = time.monotonic() + args.max_runtime_seconds
    transport = DiscordTransport(
        token,
        max_requests=args.max_requests,
        deadline_monotonic=run_deadline_monotonic,
    )
    inventory = live_inventory(
        transport,
        args.guild_id,
        entries,
        archived_page_limit=args.archived_page_limit,
    )
    application = transport.get("/oauth2/applications/@me")
    flags = int(application.get("flags", 0)) if isinstance(application, Mapping) else 0
    if flags & ((1 << 18) | (1 << 19)) == 0:
        raise BaselineError("message_content_capability_unproven")

    root_store = rich.RichArchiveStore(
        archive_root,
        lock_path=rich.canonical_archive_lock_path(archive_root),
    )
    lock_token = root_store.acquire_lock()
    run_context: rich.FullRebuildRunContext | None = None
    try:
        run_context = rich.begin_full_rebuild_run(
            fetch_inventory=lambda: inventory,
            expected_entries=inventory["entries"],
            archive_root=archive_root,
            lock_token=lock_token,
            limits=rich.AssetLimits(
                full_run_files=args.max_asset_files,
                full_run_bytes=args.max_asset_bytes,
                disk_reserve_bytes=args.minimum_free_space_bytes,
            ),
        )
        if (archive_root / "RUN_CURRENT.json").is_file():
            pointer, manifest = verify_root_pointer(archive_root)
            if manifest.get("entryCount") != len(entries):
                raise BaselineError("existing_root_manifest_scope_mismatch")
            updated = publish_compatibility(archive_root, entries, manifest, run_context)
            require_directory_identity(archive_root, archive_identity)
            require_identity(state_path, state_identity)
            require_identity(queue_path, queue_identity)
            if file_sha256(state_path) != initial_state_sha256 or file_sha256(queue_path) != initial_queue_sha256:
                raise BaselineError("state_queue_mutated")
            receipt = {
                "schemaVersion": RUN_RECEIPT_SCHEMA,
                "status": "COMMITTED",
                "mode": "RESUME_COMPATIBILITY",
                "runId": pointer["runId"],
                "entryCount": len(entries),
                "compatibilityUpdated": updated,
                "requestCount": transport.requests,
                "retryCount": transport.retries,
                "stateQueueInvariant": True,
            }
            receipt["completedAt"] = datetime.now(timezone.utc).isoformat()
            receipt["receiptSha256"] = json_sha256(receipt)
            receipt_path = rich.contained_path(
                archive_root,
                f"runs/{pointer['runId']}/receipts/full-rich-baseline.json",
            )
            receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            rich.atomic_json(receipt_path, receipt)
            return receipt

        run_id = args.run_id
        run_root = rich.contained_path(archive_root, f"runs/{run_id}")
        run_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(run_root.parent, 0o700)
        run_root.mkdir(parents=False, exist_ok=False, mode=0o700)
        os.chmod(run_root, 0o700)
        reference, evidence = immutable_reference(
            archive_root,
            state_path,
            queue_path,
            run_id,
        )
        rich.atomic_json(run_root / "immutable-pre-repair-evidence.json", evidence)
        selected: list[dict[str, Any]] = []
        for sequence, entry in enumerate(entries, 1):
            channel_id = str(entry["channelId"])
            generation_id = f"full-{run_id}-{sequence:04d}"
            store = rich.RichArchiveStore(
                safe_entry_root(archive_root, str(entry["relativePath"])),
                lock_path=rich.canonical_archive_lock_path(archive_root),
            )
            evidence_token = rich.collect_live_evidence(
                fetch_page=transport.page,
                verify_immutable_evidence=lambda ref=reference: ref,
                run_context=run_context,
                entry_root=store.entry_root,
                generation_id=generation_id,
                channel_id=channel_id,
                relative_path=str(entry["relativePath"]),
                page_limit=args.page_size,
                max_pages=args.max_pages_per_entry,
                max_messages=args.max_messages_per_entry,
                evidence_ttl_seconds=remaining_evidence_ttl(
                    run_deadline_monotonic,
                ),
            )
            stage = store.materialize_full_stage_from_live_evidence(
                generation_id=generation_id,
                live_evidence_token=evidence_token,
                downloader=rich.AssetDownloader(
                    limits=rich.AssetLimits(
                        full_run_files=args.max_asset_files,
                        full_run_bytes=args.max_asset_bytes,
                        disk_reserve_bytes=args.minimum_free_space_bytes,
                    )
                ),
            )
            store.reserve_full_stage_assets(
                stage,
                run_context=run_context,
                channel_id=channel_id,
                relative_path=str(entry["relativePath"]),
            )
            installed = store.install_full_pass_evidence(
                stage,
                live_evidence_token=evidence_token,
            )
            generation_sha256 = str(installed["manifest"]["generationSha256"])
            final = store.seal_full_stage_for_root_run(
                stage,
                generation_id,
                generation_sha256,
                live_evidence_token=evidence_token,
                run_context=run_context,
            )
            local = rich.verify_generation(final)
            receipt_path = final / "receipts/rich-archive-latest.json"
            selected.append({
                "channelId": channel_id,
                "relativePath": str(entry["relativePath"]),
                "type": str(entry["type"]),
                "generationId": generation_id,
                "generationSha256": generation_sha256,
                "receiptSha256": file_sha256(receipt_path),
                "messageCount": int(local.get("messageCount") or 0),
            })

        sealed = rich.verify_sealed_full_rebuild_run(run_context)
        if sealed.get("gateStatus") != "PASS":
            raise BaselineError("sealed_full_run_incomplete")
        manifest = {
            "schemaVersion": RUN_MANIFEST_SCHEMA,
            "runId": run_id,
            "guildId": args.guild_id,
            "entryCount": len(selected),
            "entrySetSha256": json_sha256([
                {
                    "channelId": row["channelId"],
                    "relativePath": row["relativePath"],
                    "type": row["type"],
                }
                for row in selected
            ]),
            "stateSha256": initial_state_sha256,
            "queueSha256": initial_queue_sha256,
            "sealedRunReceiptSha256": str(sealed["receiptSha256"]),
            "entries": selected,
        }
        manifest["manifestPayloadSha256"] = json_sha256(manifest)
        manifest_path = run_root / "run-manifest.json"
        rich.atomic_json(manifest_path, manifest)
        manifest_sha256 = file_sha256(manifest_path)
        root_pointer = pointer_body(run_id, manifest_sha256)
        rich.atomic_json(archive_root / "RUN_CURRENT.json", root_pointer)
        readback_pointer, readback_manifest = verify_root_pointer(archive_root)
        if readback_pointer != root_pointer or readback_manifest != manifest:
            raise BaselineError("root_readback_mismatch")
        updated = publish_compatibility(archive_root, entries, manifest, run_context)
        require_directory_identity(archive_root, archive_identity)
        require_identity(state_path, state_identity)
        require_identity(queue_path, queue_identity)
        if file_sha256(state_path) != initial_state_sha256 or file_sha256(queue_path) != initial_queue_sha256:
            raise BaselineError("state_queue_mutated")
        receipt = {
            "schemaVersion": RUN_RECEIPT_SCHEMA,
            "status": "COMMITTED",
            "mode": "FULL_BASELINE",
            "runId": run_id,
            "entryCount": len(entries),
            "messageCount": sum(int(row["messageCount"]) for row in selected),
            "rootPointerSha256": file_sha256(archive_root / "RUN_CURRENT.json"),
            "runManifestSha256": manifest_sha256,
            "compatibilityUpdated": updated,
            "requestCount": transport.requests,
            "retryCount": transport.retries,
            "stateQueueInvariant": True,
            "completedAt": datetime.now(timezone.utc).isoformat(),
        }
        receipt["receiptSha256"] = json_sha256(receipt)
        receipt_path = run_root / "receipts/full-rich-baseline.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        rich.atomic_json(receipt_path, receipt)
        return receipt
    finally:
        if run_context is not None:
            run_context.close()
        else:
            lock_token.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--openclaw-config", required=True)
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--token-env", default="DISCORD_BOT_TOKEN")
    parser.add_argument("--archived-page-limit", type=int, default=1000)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages-per-entry", type=int, default=100000)
    parser.add_argument("--max-messages-per-entry", type=int, default=10000000)
    parser.add_argument("--max-requests", type=int, default=200000)
    parser.add_argument("--max-runtime-seconds", type=int, default=86400)
    parser.add_argument("--max-asset-files", type=int, default=200000)
    parser.add_argument("--max-asset-bytes", type=int, default=200 * 1024 * 1024 * 1024)
    parser.add_argument("--minimum-free-space-bytes", type=int, default=10 * 1024 * 1024 * 1024)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = execute(args)
    except Exception as exc:
        reason = str(exc)
        if not re.fullmatch(r"[a-z0-9_]+", reason):
            reason = "unexpected_error"
        print(json.dumps({"schemaVersion": ERROR_SCHEMA, "status": "BLOCKED", "reason": reason}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
