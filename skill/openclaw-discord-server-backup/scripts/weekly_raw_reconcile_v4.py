#!/usr/bin/env python3
"""Weekly full-inventory raw reconcile with recovery and append-only closeout.

The command intentionally emits no progress messages while scanning. A caller may
send progress before invoking it and the final report after it exits, but should
not write into the audited Discord report channel during the closeout window.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote


HERE = Path(__file__).resolve().parent


def load_sibling(name: str):
    path = HERE / name
    spec = importlib.util.spec_from_file_location(f"weekly_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reconcile = load_sibling("reconcile_raw_archive_v3.py")
worker = reconcile.worker


CLASSIFICATIONS = {
    "deleted_from_discord",
    "inaccessible_or_permission_gap",
    "local_parse_artifact",
    "cross_entry_duplicate",
    "unknown",
}

EVIDENCE_SCHEMA = "openclaw-weekly-raw-pre-repair-evidence.v1"


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_any_symlink_components(path.parent)
    if os.path.lexists(path) and path.is_symlink():
        raise RuntimeError(f"managed JSON target may not be a symlink: {path}")
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp-weekly-v4", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)


def secure_copy_regular(source: Path, destination: Path) -> None:
    """Copy one stable, unlinked regular file without following symlinks.

    The source path is rebound to the opened inode after the copy. A rename,
    replacement, write, truncation, or hard-link change during capture fails the
    evidence transaction before publication.
    """
    reject_any_symlink_components(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    reject_any_symlink_components(destination.parent)
    if os.path.lexists(destination):
        raise RuntimeError(f"evidence destination already exists: {destination}")
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    temp_name: str | None = None
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(f"evidence source must be a single-link regular file: {source}")
        output_fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".copy", dir=destination.parent
        )
        try:
            os.fchmod(output_fd, 0o600)
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(output_fd, view)
                    view = view[written:]
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        after = os.fstat(source_fd)
        try:
            rebound = os.lstat(source)
        except OSError as exc:
            raise RuntimeError(f"evidence source changed during copy: {source}") from exc
        if stat_identity(before) != stat_identity(after) or stat_identity(after) != stat_identity(rebound):
            raise RuntimeError(f"evidence source changed during copy: {source}")
        os.replace(temp_name, destination)
        temp_name = None
        copied = destination.lstat()
        if not stat.S_ISREG(copied.st_mode) or copied.st_nlink != 1:
            raise RuntimeError(f"evidence copy is not a single-link regular file: {destination}")
    finally:
        os.close(source_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_manifest_path(value: Any, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError(f"invalid evidence {field}")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise RuntimeError(f"unsafe evidence {field}: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"unsafe evidence {field}: {value}")
    return path


def reject_any_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    parts = absolute.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise RuntimeError(f"symlinked managed path is not allowed: {current}")


def reject_symlink_components(root: Path, path: Path) -> None:
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"path outside managed root: {path}") from exc
    current = root
    if current.is_symlink():
        raise RuntimeError(f"symlinked managed root is not allowed: {root}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"symlink in managed path is not allowed: {current}")


def copy_tree_exact(source: Path, destination: Path) -> None:
    """Copy regular files and directories without following or accepting symlinks."""
    if not source.exists():
        destination.mkdir(parents=True, exist_ok=False)
        return
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError(f"raw evidence source must be a regular directory: {source}")
    destination.mkdir(parents=True, exist_ok=False)
    for current, dirnames, filenames in os.walk(source, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        target_dir = destination / relative
        for dirname in sorted(dirnames):
            source_dir = current_path / dirname
            if source_dir.is_symlink():
                raise RuntimeError(f"symlink in raw evidence source: {source_dir}")
            if not source_dir.is_dir():
                raise RuntimeError(f"non-directory in raw evidence source: {source_dir}")
            (target_dir / dirname).mkdir()
        for filename in sorted(filenames):
            source_file = current_path / filename
            secure_copy_regular(source_file, target_dir / filename)


def harden_read_only_tree(root: Path, *, harden_root: bool = True) -> None:
    """Make a finalized evidence tree owner-readable but non-writable."""
    for current, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for filename in filenames:
            path = current_path / filename
            file_stat = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_nlink != 1
            ):
                raise RuntimeError(f"cannot harden non-regular evidence file: {path}")
            path.chmod(0o400)
        for dirname in dirnames:
            path = current_path / dirname
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"cannot harden invalid evidence directory: {path}")
            path.chmod(0o500)
    root.chmod(0o500 if harden_root else 0o700)


def make_tree_writable_for_cleanup(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for current, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        current_path.chmod(0o700)
        for filename in filenames:
            path = current_path / filename
            if not path.is_symlink():
                path.chmod(0o600)
        for dirname in dirnames:
            path = current_path / dirname
            if not path.is_symlink():
                path.chmod(0o700)


def inventory_evidence_tree(
    bundle: Path, *, require_non_writable: bool = True
) -> tuple[list[str], list[dict[str, Any]]]:
    directories: list[str] = []
    files: list[dict[str, Any]] = []
    if bundle.is_symlink() or not bundle.is_dir():
        raise RuntimeError(f"evidence bundle must be a regular directory: {bundle}")
    if require_non_writable and bundle.lstat().st_mode & 0o222:
        raise RuntimeError(f"evidence bundle must be non-writable: {bundle}")
    for current, dirnames, filenames in os.walk(bundle, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(bundle)
        if relative_dir != Path("."):
            directories.append(relative_dir.as_posix())
        if require_non_writable and current_path.lstat().st_mode & 0o222:
            raise RuntimeError(f"evidence directory must be non-writable: {current_path}")
        for dirname in dirnames:
            path = current_path / dirname
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"invalid evidence directory: {path}")
        for filename in filenames:
            path = current_path / filename
            relative = path.relative_to(bundle).as_posix()
            if relative == "evidence-manifest.json":
                continue
            file_stat = path.lstat()
            mode = file_stat.st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or file_stat.st_nlink != 1:
                raise RuntimeError(f"invalid evidence file: {path}")
            if require_non_writable and mode & 0o222:
                raise RuntimeError(f"evidence file must be non-writable: {path}")
            files.append({
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    directories.sort()
    files.sort(key=lambda row: row["path"])
    return directories, files


def evidence_entry_dir(key: str) -> str:
    encoded = quote(key, safe="")
    if not encoded:
        raise RuntimeError("empty entry key is not allowed in evidence")
    return f"raw/{encoded}"


def create_pre_repair_evidence(
    recovery_root: Path,
    state_path: Path,
    queue_path: Path,
    archive_root: Path,
    entries: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Atomically create and immediately verify an immutable pre-repair bundle."""
    recovery_root = recovery_root.absolute()
    recovery_root.parent.mkdir(parents=True, exist_ok=True)
    reject_any_symlink_components(recovery_root.parent)
    reject_any_symlink_components(state_path)
    if queue_path.exists() or queue_path.is_symlink():
        reject_any_symlink_components(queue_path)
    reject_any_symlink_components(archive_root)
    if recovery_root.exists() or recovery_root.is_symlink():
        raise FileExistsError(f"pre-repair evidence already exists: {recovery_root}")
    stage = Path(tempfile.mkdtemp(prefix=f".{recovery_root.name}-stage-", dir=recovery_root.parent))
    try:
        for source, target_name in ((state_path, "state.json"), (queue_path, "queue.json")):
            if not source.exists():
                raise RuntimeError(f"required pre-repair evidence source is missing: {target_name}")
            secure_copy_regular(source, stage / target_name)

        manifest_entries: list[dict[str, str]] = []
        seen_keys: set[str] = set()
        seen_targets: set[str] = set()
        for key, entry in entries:
            if key in seen_keys:
                raise RuntimeError(f"duplicate evidence entry key: {key}")
            seen_keys.add(key)
            relative_value = entry.get("relativePath") or key
            if not isinstance(relative_value, str):
                raise RuntimeError(f"invalid relativePath for evidence entry: {key}")
            relative_path = relative_value
            entry_dir = safe_entry_dir(archive_root, relative_path)
            reject_symlink_components(archive_root, entry_dir)
            source = entry_dir / "raw"
            reject_symlink_components(archive_root, source)
            bundle_path = evidence_entry_dir(key)
            if bundle_path in seen_targets:
                raise RuntimeError(f"duplicate evidence bundle path: {bundle_path}")
            seen_targets.add(bundle_path)
            copy_tree_exact(source, stage / Path(bundle_path))
            manifest_entries.append({
                "entryKey": key,
                "sourceRelativePath": relative_path,
                "bundleRawPath": bundle_path,
            })

        directories, files = inventory_evidence_tree(stage, require_non_writable=False)
        manifest = {
            "schema": EVIDENCE_SCHEMA,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "entries": manifest_entries,
            "directories": directories,
            "files": files,
            "fileCount": len(files),
            "bytes": sum(int(row["bytes"]) for row in files),
        }
        atomic_json(stage / "evidence-manifest.json", manifest)
        # Keep only the staging directory itself writable long enough for an
        # atomic rename on platforms that require it. The published directory
        # is made non-writable before any verifier or repair can observe it.
        harden_read_only_tree(stage, harden_root=False)
        os.replace(stage, recovery_root)
        recovery_root.chmod(0o500)
        return verify_evidence_bundle(recovery_root)
    except Exception:
        make_tree_writable_for_cleanup(stage)
        shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_evidence_bundle(recovery_root: Path) -> dict[str, Any]:
    recovery_root = recovery_root.absolute()
    reject_any_symlink_components(recovery_root)
    if recovery_root.is_symlink() or not recovery_root.is_dir():
        raise RuntimeError(f"evidence bundle is missing or symlinked: {recovery_root}")
    manifest_path = recovery_root / "evidence-manifest.json"
    manifest_stat = manifest_path.lstat() if manifest_path.exists() else None
    mode = manifest_stat.st_mode if manifest_stat else 0
    if (
        stat.S_ISLNK(mode)
        or not stat.S_ISREG(mode)
        or manifest_stat is None
        or manifest_stat.st_nlink != 1
    ):
        raise RuntimeError("evidence manifest is missing, symlinked, or non-regular")
    if mode & 0o222:
        raise RuntimeError("evidence manifest must be non-writable")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("evidence manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != EVIDENCE_SCHEMA:
        raise RuntimeError("unsupported evidence manifest schema")
    expected_dirs = manifest.get("directories")
    expected_files = manifest.get("files")
    expected_entries = manifest.get("entries")
    if not isinstance(expected_dirs, list) or not isinstance(expected_files, list) or not isinstance(expected_entries, list):
        raise RuntimeError("evidence manifest inventory is malformed")

    normalized_dirs: list[str] = []
    for value in expected_dirs:
        normalized_dirs.append(safe_manifest_path(value, field="directory path").as_posix())
    if len(normalized_dirs) != len(set(normalized_dirs)):
        raise RuntimeError("duplicate directory in evidence manifest")

    normalized_files: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for row in expected_files:
        if not isinstance(row, dict):
            raise RuntimeError("malformed evidence file row")
        relative = safe_manifest_path(row.get("path"), field="file path").as_posix()
        if relative == "evidence-manifest.json" or relative in seen_files:
            raise RuntimeError(f"duplicate or reserved evidence file path: {relative}")
        seen_files.add(relative)
        if not isinstance(row.get("bytes"), int) or row["bytes"] < 0:
            raise RuntimeError(f"invalid evidence byte count: {relative}")
        digest = row.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise RuntimeError(f"invalid evidence digest: {relative}")
        normalized_files.append({"path": relative, "bytes": row["bytes"], "sha256": digest})

    entry_keys: set[str] = set()
    entry_paths: set[str] = set()
    for row in expected_entries:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("entryKey"), str)
            or not row["entryKey"]
        ):
            raise RuntimeError("malformed evidence entry row")
        safe_manifest_path(row.get("sourceRelativePath"), field="entry source relativePath")
        bundle_path = safe_manifest_path(row.get("bundleRawPath"), field="entry bundle path").as_posix()
        if not bundle_path.startswith("raw/") or bundle_path not in normalized_dirs:
            raise RuntimeError(f"invalid evidence entry bundle path: {bundle_path}")
        if row["entryKey"] in entry_keys or bundle_path in entry_paths:
            raise RuntimeError("duplicate evidence entry identity")
        entry_keys.add(row["entryKey"])
        entry_paths.add(bundle_path)

    actual_dirs, actual_files = inventory_evidence_tree(recovery_root)
    if sorted(normalized_dirs) != actual_dirs:
        raise RuntimeError("evidence directory inventory mismatch")
    if sorted(normalized_files, key=lambda row: row["path"]) != actual_files:
        raise RuntimeError("evidence file inventory, size, or SHA-256 mismatch")
    if manifest.get("fileCount") != len(actual_files):
        raise RuntimeError("evidence fileCount mismatch")
    if manifest.get("bytes") != sum(int(row["bytes"]) for row in actual_files):
        raise RuntimeError("evidence byte total mismatch")
    if "state.json" not in seen_files:
        raise RuntimeError("evidence state copy is missing from manifest")
    return manifest


def safe_entry_dir(root: Path, relative_path: str) -> Path:
    relative = safe_manifest_path(relative_path, field="archive relativePath")
    root_resolved = root.resolve()
    candidate = (root / Path(*relative.parts)).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise RuntimeError(f"unsafe relativePath outside archive root: {relative_path}")
    return candidate


def ordered_entries(
    state: dict[str, Any], report_entry_key: str | None = None
) -> list[tuple[str, dict[str, Any]]]:
    rows = [
        (key, entry)
        for key, entry in (state.get("entries") or {}).items()
        if not reconcile.entry_is_excluded(entry)
    ]
    rows.sort(key=lambda row: (row[0] == report_entry_key, row[0]))
    return rows


def capture_report_cutoff(
    entries: list[tuple[str, dict[str, Any]]],
    report_entry_key: str | None,
    token: str,
) -> str | None:
    """Freeze the report entry at its latest message before closeout starts.

    Progress cards sent while a long reconcile is running are intentionally left
    for the next incremental scope instead of creating an endless moving target.
    """
    if report_entry_key is None:
        return None
    entry = next((row for key, row in entries if key == report_entry_key), None)
    if entry is None:
        raise RuntimeError(f"report entry is not registered: {report_entry_key}")
    channel_id = entry.get("channelId")
    if not channel_id:
        raise RuntimeError(f"report entry has no channelId: {report_entry_key}")
    latest = worker.discord_messages(token, str(channel_id), after=None, limit=1)
    return max((str(message["id"]) for message in latest), key=int, default="0")


def within_cutoff(message_id: str, cutoff: str | None) -> bool:
    return cutoff is None or int(message_id) <= int(cutoff)


def scan(
    entries: list[tuple[str, dict[str, Any]]],
    root: Path,
    token: str,
    page_limit: int,
    report_entry_key: str | None = None,
    report_cutoff_message_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, set[str]]]:
    rows: list[dict[str, Any]] = []
    messages_by_key: dict[str, list[dict[str, Any]]] = {}
    raw_ids_by_key: dict[str, set[str]] = {}
    for key, entry in entries:
        relative_path = str(entry.get("relativePath") or key)
        raw_dir = safe_entry_dir(root, relative_path) / "raw"
        raw_counts, _ = reconcile.archive_message_ids(raw_dir)
        cutoff = report_cutoff_message_id if key == report_entry_key else None
        raw_ids = {
            message_id for message_id in raw_counts
            if within_cutoff(message_id, cutoff)
        }
        raw_ids_by_key[key] = raw_ids
        row: dict[str, Any] = {
            "key": key,
            "channelId": entry.get("channelId"),
            "relativePath": relative_path,
            "rawMessageIds": len(raw_ids),
            "duplicateRawIds": sum(
                count - 1
                for message_id, count in raw_counts.items()
                if message_id in raw_ids and count > 1
            ),
        }
        if not entry.get("channelId"):
            row["liveError"] = "missing_channel_id"
            rows.append(row)
            continue
        try:
            messages = [
                message
                for message in reconcile.fetch_all_messages(
                    token, str(entry["channelId"]), page_limit
                )
                if within_cutoff(str(message["id"]), cutoff)
            ]
            messages_by_key[key] = messages
            live_ids = {str(message["id"]) for message in messages}
            missing = [message for message in messages if str(message["id"]) not in raw_ids]
            row.update({
                "liveMessages": len(live_ids),
                "liveOnly": len(missing),
                "localOnly": len(raw_ids - live_ids),
            })
        except Exception as exc:
            row["liveError"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    return rows, messages_by_key, raw_ids_by_key


def apply_missing(
    state: dict[str, Any],
    queue: dict[str, Any],
    entries: list[tuple[str, dict[str, Any]]],
    root: Path,
    messages_by_key: dict[str, list[dict[str, Any]]],
    raw_ids_by_key: dict[str, set[str]],
    today: str,
    recovery_root: Path,
    state_path: Path,
    queue_path: Path,
    copied: set[str],
) -> tuple[int, list[str]]:
    appended = 0
    affected: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    missing_by_key = {
        key: [
            message
            for message in (messages_by_key.get(key) or [])
            if str(message["id"]) not in raw_ids_by_key.get(key, set())
        ]
        for key, _entry in entries
    }
    if any(missing_by_key.values()):
        if not copied:
            create_pre_repair_evidence(
                recovery_root, state_path, queue_path, root, entries
            )
            copied.update(key for key, _entry in entries)
        else:
            verify_evidence_bundle(recovery_root)
    for key, entry in entries:
        messages = messages_by_key.get(key) or []
        missing = missing_by_key[key]
        if not missing:
            continue
        # Verification immediately precedes every append. If evidence is missing,
        # extra, symlinked, traversing, or changed, no repair write is attempted.
        verify_evidence_bundle(recovery_root)
        worker.append_batch(root, entry, missing, f"weekly full-inventory repair {now}")
        latest = max((str(message["id"]) for message in messages), key=int)
        entry.update({
            "lastWrittenMessageId": latest,
            "lastMessageId": latest,
            "lastBackup": today,
            "lastSuccessfulWriteAt": now,
            "syncStatus": "healthy",
            "backlogReason": None,
            "consecutiveErrors": 0,
            "updatedAt": now,
        })
        reconcile.mark_queue_caught_up(queue, key, latest, now)
        appended += len(missing)
        affected.append(key)
    if affected:
        state["updatedAt"] = now
        queue["updatedAt"] = now
        atomic_json(state_path, state)
        atomic_json(queue_path, queue)
    return appended, affected


def classify_local_only(
    rows: list[dict[str, Any]],
    messages_by_key: dict[str, list[dict[str, Any]]],
    raw_ids_by_key: dict[str, set[str]],
) -> dict[str, Any]:
    live_ids_by_key = {
        key: {str(message["id"]) for message in messages}
        for key, messages in messages_by_key.items()
    }
    raw_owners: dict[str, set[str]] = defaultdict(set)
    for key, ids in raw_ids_by_key.items():
        for message_id in ids:
            raw_owners[message_id].add(key)
    errors = {row["key"] for row in rows if row.get("liveError")}
    details: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    for key, raw_ids in raw_ids_by_key.items():
        for message_id in sorted(raw_ids - live_ids_by_key.get(key, set()), key=int):
            if key in errors:
                kind = "inaccessible_or_permission_gap"
            elif len(raw_owners[message_id]) > 1:
                kind = "cross_entry_duplicate"
            elif key in live_ids_by_key:
                kind = "deleted_from_discord"
            elif not message_id.isdigit():
                kind = "local_parse_artifact"
            else:
                kind = "unknown"
            counts[kind] += 1
            details.append({"entry": key, "messageId": message_id, "classification": kind})
    for kind in CLASSIFICATIONS:
        counts.setdefault(kind, 0)
    live_union = set().union(*live_ids_by_key.values()) if live_ids_by_key else set()
    raw_union = set().union(*raw_ids_by_key.values()) if raw_ids_by_key else set()
    return {
        "counts": dict(sorted(counts.items())),
        "details": details,
        "liveMessageIds": len(live_union),
        "localRawMessageIds": len(raw_union),
        "intersectionMessageIds": len(live_union & raw_union),
        "liveOnlyMessageIds": len(live_union - raw_union),
        "localOnlyMessageIds": len(raw_union - live_union),
        "setConservationPass": (
            len(live_union | raw_union)
            == len(live_union & raw_union) + len(live_union - raw_union) + len(raw_union - live_union)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run recovery-first weekly raw reconcile and full closeout.")
    parser.add_argument("--state")
    parser.add_argument("--queue")
    parser.add_argument("--root")
    parser.add_argument("--today")
    parser.add_argument("--evidence-dir")
    parser.add_argument(
        "--verify-evidence",
        help="Verify one immutable pre-repair bundle and exit without Discord or archive writes.",
    )
    parser.add_argument("--openclaw-config", default=str(Path.home() / ".openclaw/openclaw.json"))
    parser.add_argument("--token-env", default="DISCORD_BOT_TOKEN")
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--max-closeout-passes", type=int, default=3)
    parser.add_argument("--report-entry-key")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    if args.verify_evidence:
        manifest = verify_evidence_bundle(Path(args.verify_evidence))
        print(json.dumps({
            "ok": True,
            "schema": manifest["schema"],
            "fileCount": manifest["fileCount"],
            "bytes": manifest["bytes"],
        }, ensure_ascii=False, indent=2))
        return 0
    for field in ("state", "queue", "root", "today", "evidence_dir"):
        if getattr(args, field) is None:
            parser.error(f"--{field.replace('_', '-')} is required unless --verify-evidence is used")
    if not 1 <= args.page_limit <= 100:
        parser.error("--page-limit must be between 1 and 100")
    if not 1 <= args.max_closeout_passes <= 10:
        parser.error("--max-closeout-passes must be between 1 and 10")

    state_path = Path(args.state)
    queue_path = Path(args.queue)
    root = Path(args.root)
    evidence_dir = Path(args.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    state = reconcile.load_json(state_path)
    queue = reconcile.load_json(queue_path, {"version": 1, "items": []})
    entries = ordered_entries(state, args.report_entry_key)
    token = worker.load_discord_token(Path(args.openclaw_config), args.token_env)

    lock_path = state_path.parent / ".channel_backup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        print(json.dumps({"ok": False, "skipped": "locked"}, ensure_ascii=False))
        return 2

    copied: set[str] = set()
    passes: list[dict[str, Any]] = []
    total_appended = 0
    try:
        report_cutoff_message_id = capture_report_cutoff(
            entries, args.report_entry_key, token
        )
        for pass_number in range(1, args.max_closeout_passes + 1):
            rows, messages_by_key, raw_ids_by_key = scan(
                entries,
                root,
                token,
                args.page_limit,
                args.report_entry_key,
                report_cutoff_message_id,
            )
            live_errors = sum(bool(row.get("liveError")) for row in rows)
            live_only = sum(int(row.get("liveOnly") or 0) for row in rows)
            pass_result = {
                "pass": pass_number,
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "entries": len(rows),
                "liveErrors": live_errors,
                "liveOnlyBeforeRepair": live_only,
            }
            if live_errors:
                passes.append(pass_result)
                break
            appended, affected = apply_missing(
                state, queue, entries, root, messages_by_key, raw_ids_by_key,
                args.today, evidence_dir / "pre-repair", state_path, queue_path, copied,
            )
            total_appended += appended
            pass_result.update({"appended": appended, "affectedEntries": affected})
            passes.append(pass_result)
            if live_only == 0:
                break

        final_rows, final_messages, final_raw_ids = scan(
            entries,
            root,
            token,
            args.page_limit,
            args.report_entry_key,
            report_cutoff_message_id,
        )
        final_live_errors = sum(bool(row.get("liveError")) for row in final_rows)
        final_live_only = sum(int(row.get("liveOnly") or 0) for row in final_rows)
        classification = classify_local_only(final_rows, final_messages, final_raw_ids)
        state_entries = state.get("entries") or {}
        active_queue = sum(
            1
            for item in queue.get("items", [])
            if item.get("status") in reconcile.ACTIVE_QUEUE
            and not reconcile.entry_is_excluded(state_entries.get(item.get("entryKey"), {}))
        )
        ok = (
            final_live_errors == 0
            and final_live_only == 0
            and active_queue == 0
            and classification["counts"]["unknown"] == 0
            and classification["setConservationPass"]
        )
        result = {
            "schema": "openclaw-weekly-raw-reconcile-v4",
            "ok": ok,
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "entries": len(entries),
            "excludedEntries": len((state.get("entries") or {})) - len(entries),
            "passes": passes,
            "appended": total_appended,
            "finalLiveOnly": final_live_only,
            "finalLiveErrors": final_live_errors,
            "activeQueue": active_queue,
            "reportEntryKey": args.report_entry_key,
            "reportEntryCutoffMessageId": report_cutoff_message_id,
            "localOnlyClassification": classification,
            "recoveryPath": str(evidence_dir / "pre-repair") if copied else None,
            "selfDriftGuard": "Report-entry messages newer than the frozen cutoff belong to the next incremental scope.",
        }
        atomic_json(evidence_dir / "weekly-reconciliation-summary.json", result)
        atomic_json(evidence_dir / "local-only-id-classification.json", classification)
        if args.compact:
            print(
                "[weekly-raw-v4] "
                f"ok={str(ok).lower()} entries={len(entries)} appended={total_appended} "
                f"liveOnly={final_live_only} liveErrors={final_live_errors} "
                f"localOnly={classification['localOnlyMessageIds']} unknown={classification['counts']['unknown']} "
                f"activeQueue={active_queue}"
            )
        else:
            print(json.dumps({key: value for key, value in result.items() if key != "localOnlyClassification"}, ensure_ascii=False, indent=2))
        return 0 if ok else 2
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
