#!/usr/bin/env python3
"""Deterministic core-workspace backup, verification, and restore canary.

Scope is intentionally narrow: root-level Markdown files plus the complete
workspace memory/ tree. The tool never restores into a live workspace.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "openclaw-core-workspace-backup-v1"
MANIFEST_NAME = ".backup-manifest.json"
CHUNK_SIZE = 1024 * 1024


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceFile:
    relative_path: str
    source: Path
    size: int
    sha256: str


def lexical_absolute(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                digest.update(chunk)
    except OSError as exc:
        raise BackupError(f"cannot hash {path.name}: {exc}") from exc
    return digest.hexdigest()


def validate_relative_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise BackupError(f"invalid {label} path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise BackupError(f"unsafe {label} path: {value}")
    if value == MANIFEST_NAME:
        raise BackupError(f"reserved {label} path: {value}")
    return value


def assert_regular_source(path: Path, relative_path: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError(f"cannot stat source {relative_path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise BackupError(f"source symlink is not allowed: {relative_path}")
    if not stat.S_ISREG(info.st_mode):
        raise BackupError(f"source is not a regular file: {relative_path}")
    return info


def collect_source(workspace: Path) -> tuple[list[SourceFile], list[str], int]:
    if workspace.is_symlink() or not workspace.is_dir():
        raise BackupError("workspace must be a readable directory, not a symlink")
    memory = workspace / "memory"
    if memory.is_symlink() or not memory.is_dir():
        raise BackupError("workspace memory/ must be a readable directory, not a symlink")

    source_paths: list[tuple[str, Path]] = []
    root_md_count = 0
    try:
        root_entries = sorted(workspace.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise BackupError(f"cannot list workspace: {exc}") from exc
    for item in root_entries:
        if item.name.endswith(".md"):
            assert_regular_source(item, item.name)
            source_paths.append((item.name, item))
            root_md_count += 1

    directories = ["memory"]
    for current, dir_names, file_names in os.walk(memory, topdown=True, followlinks=False):
        current_path = Path(current)
        dir_names.sort()
        file_names.sort()
        for name in list(dir_names):
            child = current_path / name
            relative = child.relative_to(workspace).as_posix()
            try:
                info = child.lstat()
            except OSError as exc:
                raise BackupError(f"cannot stat source directory {relative}: {exc}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise BackupError(f"source symlink is not allowed: {relative}")
            if not stat.S_ISDIR(info.st_mode):
                raise BackupError(f"source tree contains a non-directory entry: {relative}")
            directories.append(relative)
        for name in file_names:
            child = current_path / name
            relative = child.relative_to(workspace).as_posix()
            assert_regular_source(child, relative)
            source_paths.append((relative, child))

    files: list[SourceFile] = []
    for relative, source in sorted(source_paths):
        info_before = assert_regular_source(source, relative)
        digest = sha256_path(source)
        info_after = assert_regular_source(source, relative)
        if (info_before.st_dev, info_before.st_ino, info_before.st_size, info_before.st_mtime_ns) != (
            info_after.st_dev,
            info_after.st_ino,
            info_after.st_size,
            info_after.st_mtime_ns,
        ):
            raise BackupError(f"source changed while being inventoried: {relative}")
        files.append(SourceFile(relative, source, info_after.st_size, digest))
    return files, sorted(set(directories)), root_md_count


def manifest_payload(files: list[SourceFile], directories: list[str], root_md_count: int) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "rootMarkdownCount": root_md_count,
        "directories": directories,
        "files": [
            {"path": item.relative_path, "bytes": item.size, "sha256": item.sha256}
            for item in files
        ],
    }


def copy_regular_file(source: Path, destination: Path, expected: SourceFile) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise BackupError(f"cannot safely open source {expected.relative_path}: {exc}") from exc
    try:
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode):
            raise BackupError(f"source is no longer a regular file: {expected.relative_path}")
        with os.fdopen(source_fd, "rb", closefd=False) as source_handle:
            with destination.open("xb") as destination_handle:
                shutil.copyfileobj(source_handle, destination_handle, CHUNK_SIZE)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
    finally:
        os.close(source_fd)
    if destination.stat().st_size != expected.size or sha256_path(destination) != expected.sha256:
        raise BackupError(f"source changed while being copied: {expected.relative_path}")


def write_manifest(root: Path, payload: dict[str, Any]) -> None:
    temp_path = root / (MANIFEST_NAME + ".tmp")
    final_path = root / MANIFEST_NAME
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temp_path.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(final_path)


def build_tree(root: Path, files: list[SourceFile], directories: list[str], root_md_count: int) -> None:
    root.mkdir(parents=False, exist_ok=False)
    for relative in directories:
        (root / Path(*PurePosixPath(relative).parts)).mkdir(parents=True, exist_ok=False)
    for item in files:
        destination = root / Path(*PurePosixPath(item.relative_path).parts)
        copy_regular_file(item.source, destination, item)
    write_manifest(root, manifest_payload(files, directories, root_md_count))
    verify_tree(root)


def load_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_symlink():
        raise BackupError("manifest symlink is not allowed")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"manifest is not readable JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise BackupError("unsupported or invalid manifest schema")
    return data


def validate_manifest(data: dict[str, Any]) -> tuple[dict[str, tuple[int, str]], set[str], int]:
    raw_files = data.get("files")
    raw_directories = data.get("directories")
    root_md_count = data.get("rootMarkdownCount")
    if not isinstance(raw_files, list) or not isinstance(raw_directories, list):
        raise BackupError("manifest files/directories must be arrays")
    if not isinstance(root_md_count, int) or root_md_count < 0:
        raise BackupError("manifest rootMarkdownCount is invalid")

    files: dict[str, tuple[int, str]] = {}
    for row in raw_files:
        if not isinstance(row, dict):
            raise BackupError("manifest file entry must be an object")
        relative = validate_relative_path(row.get("path"), label="file")
        size = row.get("bytes")
        digest = row.get("sha256")
        if relative in files:
            raise BackupError(f"duplicate manifest file path: {relative}")
        if not isinstance(size, int) or size < 0:
            raise BackupError(f"invalid byte count for {relative}")
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise BackupError(f"invalid sha256 for {relative}")
        files[relative] = (size, digest)

    directories: set[str] = set()
    for value in raw_directories:
        relative = validate_relative_path(value, label="directory")
        if relative in directories:
            raise BackupError(f"duplicate manifest directory path: {relative}")
        directories.add(relative)
    if "memory" not in directories:
        raise BackupError("manifest does not include memory/")
    if any(path in directories for path in files):
        raise BackupError("manifest path is both file and directory")
    if sum(1 for path in files if "/" not in path and path.endswith(".md")) != root_md_count:
        raise BackupError("manifest rootMarkdownCount does not match files")
    return files, directories, root_md_count


def actual_tree(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    for current, dir_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dir_names.sort()
        file_names.sort()
        for name in list(dir_names):
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise BackupError(f"backup symlink is not allowed: {relative}")
            if not stat.S_ISDIR(info.st_mode):
                raise BackupError(f"backup entry is not a directory: {relative}")
            directories.add(relative)
        for name in file_names:
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise BackupError(f"backup symlink is not allowed: {relative}")
            if not stat.S_ISREG(info.st_mode):
                raise BackupError(f"backup entry is not a regular file: {relative}")
            if relative != MANIFEST_NAME:
                files.add(relative)
    return files, directories


def verify_tree(root: Path) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise BackupError("backup root must be a directory, not a symlink")
    data = load_manifest(root)
    expected_files, expected_directories, root_md_count = validate_manifest(data)
    found_files, found_directories = actual_tree(root)
    missing_files = sorted(set(expected_files) - found_files)
    extra_files = sorted(found_files - set(expected_files))
    missing_directories = sorted(expected_directories - found_directories)
    extra_directories = sorted(found_directories - expected_directories)
    if missing_files or extra_files or missing_directories or extra_directories:
        raise BackupError(
            "tree mismatch: "
            f"missing_files={missing_files}, extra_files={extra_files}, "
            f"missing_directories={missing_directories}, extra_directories={extra_directories}"
        )
    for relative, (expected_size, expected_hash) in expected_files.items():
        path = root / Path(*PurePosixPath(relative).parts)
        if path.stat().st_size != expected_size:
            raise BackupError(f"size mismatch: {relative}")
        if sha256_path(path) != expected_hash:
            raise BackupError(f"sha256 mismatch: {relative}")
    return {
        "status": "verified",
        "files": len(expected_files),
        "directories": len(expected_directories),
        "rootMarkdownCount": root_md_count,
    }


def ensure_directory_no_symlink(path: Path, *, parents: bool = False) -> None:
    if os.path.lexists(path):
        try:
            info = path.lstat()
        except OSError as exc:
            raise BackupError(f"cannot inspect backup directory {path.name}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise BackupError(f"backup directory must not be a symlink or special file: {path.name}")
        return
    try:
        path.mkdir(parents=parents, exist_ok=False)
    except OSError as exc:
        raise BackupError(f"cannot create backup directory {path.name}: {exc}") from exc


@contextmanager
def core_backup_lock(core_root: Path):
    lock_path = core_root / ".core-backup.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("another core workspace backup is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_non_overlapping(workspace: Path, backup_root: Path) -> tuple[Path, Path]:
    try:
        resolved_workspace = workspace.resolve(strict=True)
    except OSError as exc:
        raise BackupError(f"workspace cannot be resolved: {exc}") from exc
    resolved_backup = backup_root.resolve(strict=False)
    if (
        resolved_workspace == resolved_backup
        or resolved_backup.is_relative_to(resolved_workspace)
        or resolved_workspace.is_relative_to(resolved_backup)
    ):
        raise BackupError("workspace and backup root must not overlap")
    return resolved_workspace, resolved_backup


def validate_snapshot_date(value: str) -> str:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise BackupError("snapshot date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise BackupError("snapshot date must be YYYY-MM-DD")
    return value


def replace_latest(candidate: Path, latest: Path) -> None:
    old = latest.parent / f".latest-old-{uuid.uuid4().hex}"
    moved_old = False
    if os.path.lexists(latest):
        if latest.is_symlink() or not latest.is_dir():
            raise BackupError("existing latest path is not a safe directory")
        latest.rename(old)
        moved_old = True
    try:
        candidate.rename(latest)
    except Exception as exc:
        if moved_old and not latest.exists() and old.exists():
            old.rename(latest)
        raise BackupError(f"failed to replace latest backup: {exc}") from exc
    if moved_old:
        shutil.rmtree(old)


def copy_verified_tree(source: Path, destination: Path) -> None:
    verify_tree(source)
    shutil.copytree(source, destination, symlinks=True)
    verify_tree(destination)


def run_backup(workspace: Path, backup_root: Path, snapshot_date: str) -> dict[str, Any]:
    workspace, backup_root = ensure_non_overlapping(workspace, backup_root)
    ensure_directory_no_symlink(backup_root, parents=True)
    core_root = backup_root / "核心文件"
    ensure_directory_no_symlink(core_root)
    with core_backup_lock(core_root):
        files, directories, root_md_count = collect_source(workspace)
        candidate = core_root / f".latest-stage-{uuid.uuid4().hex}"
        snapshot_stage: Path | None = None
        snapshots_root = core_root / "snapshots"
        ensure_directory_no_symlink(snapshots_root)
        snapshot = snapshots_root / snapshot_date
        latest = core_root / "latest"
        snapshot_status = "existing"
        try:
            build_tree(candidate, files, directories, root_md_count)
            if os.path.lexists(snapshot):
                if snapshot.is_symlink() or not snapshot.is_dir():
                    raise BackupError("existing snapshot path is not a safe directory")
                verify_tree(snapshot)
            else:
                snapshot_stage = snapshots_root / f".{snapshot_date}-stage-{uuid.uuid4().hex}"
                copy_verified_tree(candidate, snapshot_stage)
                try:
                    snapshot_stage.rename(snapshot)
                    snapshot_stage = None
                    snapshot_status = "created"
                except OSError:
                    # Another writer may have won the immutable snapshot race.
                    if os.path.lexists(snapshot):
                        verify_tree(snapshot)
                        snapshot_status = "existing"
                    else:
                        raise
            replace_latest(candidate, latest)
            verify_tree(latest)
            verify_tree(snapshot)
        finally:
            if candidate.exists() and candidate != latest:
                shutil.rmtree(candidate, ignore_errors=True)
            if snapshot_stage is not None and snapshot_stage.exists():
                shutil.rmtree(snapshot_stage, ignore_errors=True)

    return {
        "status": "complete",
        "latest": str(latest),
        "snapshot": str(snapshot),
        "snapshotStatus": snapshot_status,
        "files": len(files),
        "rootMarkdownCount": root_md_count,
        "memoryFiles": sum(1 for item in files if item.relative_path.startswith("memory/")),
    }


def run_restore_canary(source: Path) -> dict[str, Any]:
    before = verify_tree(source)
    with tempfile.TemporaryDirectory(prefix="openclaw-core-restore-canary-") as tmp:
        restored = Path(tmp) / "restored"
        copy_verified_tree(source, restored)
        restored_result = verify_tree(restored)
    after = verify_tree(source)
    if before != after or before != restored_result:
        raise BackupError("restore canary verification changed unexpectedly")
    return {**before, "restoreCanary": "passed"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="create latest and immutable daily snapshot")
    backup.add_argument("--workspace", required=True)
    backup.add_argument("--backup-root", required=True)
    backup.add_argument("--date", default=dt.datetime.now().astimezone().date().isoformat())

    verify = sub.add_parser("verify", help="verify exact tree and SHA-256 manifest")
    verify.add_argument("--backup-dir", required=True)

    canary = sub.add_parser("restore-canary", help="verify an isolated temporary restore")
    canary.add_argument("--backup-dir", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "backup":
            result = run_backup(
                lexical_absolute(args.workspace),
                lexical_absolute(args.backup_root),
                validate_snapshot_date(args.date),
            )
        elif args.command == "verify":
            result = verify_tree(lexical_absolute(args.backup_dir))
        else:
            result = run_restore_canary(lexical_absolute(args.backup_dir))
    except (BackupError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
