#!/usr/bin/env python3
"""Create, verify, and restore-canary immutable workspace recovery snapshots."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SNAPSHOT_SCHEMA = "openclaw-workspace-recovery-snapshot.v2"
DEFAULT_EXCLUDES = (
    ".git",
    "node_modules",
    "__pycache__",
    ".env",
    "*.pyc",
    ".DS_Store",
)


def is_excluded(relative_path: Path, patterns: Iterable[str]) -> bool:
    parts = relative_path.parts
    for pattern in patterns:
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
        if fnmatch.fnmatch(relative_path.as_posix(), pattern):
            return True
    return False


def safe_relative(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError(f"invalid {label}")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise RuntimeError(f"unsafe {label}: {value}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RuntimeError(f"unsafe {label}: {value}")
    return Path(*pure.parts)


def validate_today(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("--today must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise RuntimeError("--today must be an ISO date")
    return value


def validate_includes(values: Iterable[str]) -> list[Path]:
    normalized: list[Path] = []
    seen: set[str] = set()
    for value in values:
        relative = safe_relative(value, label="include path")
        identity = relative.as_posix()
        if identity in seen:
            raise RuntimeError(f"duplicate include path: {identity}")
        seen.add(identity)
        normalized.append(relative)
    for index, left in enumerate(normalized):
        for right in normalized[index + 1:]:
            if left in right.parents or right in left.parents:
                raise RuntimeError(
                    "snapshot include paths may not overlap: "
                    f"{left.as_posix()} and {right.as_posix()}"
                )
    return normalized


def reject_symlink_components(path: Path) -> None:
    path = path.absolute()
    parts = path.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise RuntimeError(f"symlinked managed path is not allowed: {current}")


def assert_disjoint(workspace: Path, destination: Path) -> None:
    workspace_real = workspace.resolve()
    destination_real = destination.resolve(strict=False)
    if (
        workspace_real == destination_real
        or workspace_real in destination_real.parents
        or destination_real in workspace_real.parents
    ):
        raise RuntimeError("workspace and snapshot destination must not overlap")


def validate_create_inputs(
    workspace: Path,
    destination: Path,
    includes: Iterable[str],
    excludes: Iterable[str],
    *,
    root_markdown: bool,
) -> list[Path]:
    reject_symlink_components(workspace)
    reject_symlink_components(destination)
    if workspace.is_symlink() or not workspace.is_dir():
        raise RuntimeError("workspace must be a regular directory")
    assert_disjoint(workspace, destination)
    normalized = validate_includes(includes)
    patterns = list(excludes)
    for relative in normalized:
        source = workspace / relative
        reject_symlink_components(source)
        if not source.exists():
            raise RuntimeError(f"configured snapshot include is missing: {relative.as_posix()}")
        validate_source_tree(source, patterns)
    if root_markdown:
        for path in workspace.glob("*.md"):
            if is_excluded(Path(path.name), patterns):
                continue
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                raise RuntimeError(f"root Markdown source is symlinked or non-regular: {path}")
    return normalized


def iter_files(source: Path, patterns: Iterable[str]) -> Iterable[Path]:
    """Backward-compatible filtered file iterator; never follows symlinks."""
    if source.is_file():
        if not source.is_symlink() and not is_excluded(Path(source.name), patterns):
            yield source
        return
    for root, dirnames, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        dirnames[:] = [
            name
            for name in dirnames
            if not (root_path / name).is_symlink()
            and not is_excluded(relative_root / name, patterns)
        ]
        for filename in sorted(filenames):
            path = root_path / filename
            relative = path.relative_to(source)
            if path.is_symlink() or is_excluded(relative, patterns):
                continue
            yield path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)


def secure_copy_regular(source: Path, destination: Path) -> None:
    """Copy a stable, single-link regular source into a new private file."""
    reject_symlink_components(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_components(destination.parent)
    if os.path.lexists(destination):
        raise RuntimeError(f"snapshot destination already exists: {destination}")
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    temp_name: str | None = None
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(f"snapshot source must be a single-link regular file: {source}")
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
            raise RuntimeError(f"snapshot source changed during copy: {source}") from exc
        if stat_identity(before) != stat_identity(after) or stat_identity(after) != stat_identity(rebound):
            raise RuntimeError(f"snapshot source changed during copy: {source}")
        os.replace(temp_name, destination)
        temp_name = None
        copied = destination.lstat()
        if not stat.S_ISREG(copied.st_mode) or copied.st_nlink != 1:
            raise RuntimeError(f"snapshot copy is not a single-link regular file: {destination}")
    finally:
        os.close(source_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def validate_source_tree(source: Path, patterns: Iterable[str]) -> None:
    if source.is_symlink():
        raise RuntimeError(f"snapshot source may not be a symlink: {source}")
    if source.is_file():
        source_stat = source.lstat()
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
            raise RuntimeError(f"snapshot source is not a regular file: {source}")
        return
    if not source.is_dir():
        raise RuntimeError(f"snapshot source is not a regular directory: {source}")
    for root, dirnames, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            path = root_path / dirname
            relative = relative_root / dirname
            if is_excluded(relative, patterns):
                continue
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"symlink or non-directory in snapshot source: {path}")
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in filenames:
            path = root_path / filename
            relative = relative_root / filename
            if is_excluded(relative, patterns):
                continue
            mode = path.lstat().st_mode
            file_stat = path.lstat()
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or file_stat.st_nlink != 1:
                raise RuntimeError(f"symlink or non-regular file in snapshot source: {path}")


def copy_source(source: Path, destination: Path, patterns: Iterable[str]) -> list[dict[str, Any]]:
    """Copy one source without symlinks and return source-relative file metadata."""
    validate_source_tree(source, patterns)
    rows: list[dict[str, Any]] = []
    if source.is_file():
        if is_excluded(Path(source.name), patterns):
            return rows
        destination.parent.mkdir(parents=True, exist_ok=True)
        secure_copy_regular(source, destination)
        return [{
            "path": source.name,
            "bytes": destination.stat().st_size,
            "sha256": sha256_file(destination),
        }]
    destination.mkdir(parents=True, exist_ok=True)
    for root, dirnames, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        kept_dirs: list[str] = []
        for name in dirnames:
            path = root_path / name
            if is_excluded(relative_root / name, patterns):
                continue
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"snapshot source changed during copy: {path}")
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        target_dir = destination / relative_root
        target_dir.mkdir(parents=True, exist_ok=True)
        for dirname in dirnames:
            (target_dir / dirname).mkdir(exist_ok=True)
        for filename in sorted(filenames):
            path = root_path / filename
            relative = path.relative_to(source)
            if is_excluded(relative, patterns):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            secure_copy_regular(path, target)
            rows.append({
                "path": relative.as_posix(),
                "bytes": target.stat().st_size,
                "sha256": sha256_file(target),
            })
    return rows


def inventory_snapshot_tree(snapshot: Path) -> tuple[list[str], list[dict[str, Any]]]:
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise RuntimeError(f"snapshot is missing, symlinked, or non-directory: {snapshot}")
    directories: list[str] = []
    files: list[dict[str, Any]] = []
    for root, dirnames, filenames in os.walk(snapshot, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(snapshot)
        if relative_root != Path("."):
            directories.append(relative_root.as_posix())
        for dirname in dirnames:
            path = root_path / dirname
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"invalid snapshot directory: {path}")
        for filename in filenames:
            path = root_path / filename
            relative = path.relative_to(snapshot).as_posix()
            if relative == "manifest.json":
                continue
            file_stat = path.lstat()
            mode = file_stat.st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or file_stat.st_nlink != 1:
                raise RuntimeError(f"invalid snapshot file: {path}")
            files.append({
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    directories.sort()
    files.sort(key=lambda row: row["path"])
    return directories, files


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_components(path.parent)
    if os.path.lexists(path) and path.is_symlink():
        raise RuntimeError(f"managed JSON target may not be a symlink: {path}")
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp-workspace-snapshot", dir=path.parent
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


def normalized_manifest_inventory(manifest: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    directories = manifest.get("directories")
    files = manifest.get("files")
    sources = manifest.get("sources")
    if not isinstance(directories, list) or not isinstance(files, list) or not isinstance(sources, list):
        raise RuntimeError("snapshot manifest inventory is malformed")
    normalized_dirs = [safe_relative(row, label="manifest directory").as_posix() for row in directories]
    if len(normalized_dirs) != len(set(normalized_dirs)):
        raise RuntimeError("duplicate snapshot manifest directory")
    normalized_files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in files:
        if not isinstance(row, dict):
            raise RuntimeError("malformed snapshot manifest file")
        relative = safe_relative(row.get("path"), label="manifest file").as_posix()
        if relative == "manifest.json" or relative in seen:
            raise RuntimeError(f"duplicate or reserved snapshot file: {relative}")
        seen.add(relative)
        size = row.get("bytes")
        digest = row.get("sha256")
        if not isinstance(size, int) or size < 0:
            raise RuntimeError(f"invalid snapshot byte count: {relative}")
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise RuntimeError(f"invalid snapshot SHA-256: {relative}")
        normalized_files.append({"path": relative, "bytes": size, "sha256": digest})
    seen_sources: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("source"), str):
            raise RuntimeError("malformed snapshot source declaration")
        if source.get("missing"):
            raise RuntimeError("snapshot manifest contains a missing configured source")
        source_name = source["source"]
        if source_name in seen_sources:
            raise RuntimeError(f"duplicate snapshot source declaration: {source_name}")
        seen_sources.add(source_name)
        if source_name != "root_md":
            safe_relative(source_name, label="snapshot source")
        source_files = source.get("files")
        if not isinstance(source_files, list):
            raise RuntimeError(f"malformed file list for snapshot source: {source_name}")
        source_paths: set[str] = set()
        for row in source_files:
            if not isinstance(row, dict):
                raise RuntimeError(f"malformed file row for snapshot source: {source_name}")
            source_path = safe_relative(row.get("path"), label="snapshot source file").as_posix()
            if source_path in source_paths:
                raise RuntimeError(f"duplicate source file declaration: {source_name}/{source_path}")
            source_paths.add(source_path)
            size = row.get("bytes")
            digest = row.get("sha256")
            if not isinstance(size, int) or size < 0:
                raise RuntimeError(f"invalid source file byte count: {source_name}/{source_path}")
            if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise RuntimeError(f"invalid source file SHA-256: {source_name}/{source_path}")
    return sorted(normalized_dirs), sorted(normalized_files, key=lambda row: row["path"])


def verify_snapshot(snapshot: Path) -> dict[str, Any]:
    snapshot = snapshot.absolute()
    reject_symlink_components(snapshot)
    manifest_path = snapshot / "manifest.json"
    manifest_stat = manifest_path.lstat() if manifest_path.exists() else None
    if (
        manifest_stat is None
        or manifest_path.is_symlink()
        or not stat.S_ISREG(manifest_stat.st_mode)
        or manifest_stat.st_nlink != 1
    ):
        raise RuntimeError("snapshot manifest is missing, symlinked, or non-regular")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("snapshot manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SNAPSHOT_SCHEMA:
        raise RuntimeError("unsupported snapshot manifest schema")
    expected_dirs, expected_files = normalized_manifest_inventory(manifest)
    actual_dirs, actual_files = inventory_snapshot_tree(snapshot)
    if expected_dirs != actual_dirs:
        raise RuntimeError("snapshot directory inventory mismatch")
    if expected_files != actual_files:
        raise RuntimeError("snapshot file inventory, size, or SHA-256 mismatch")
    if manifest.get("fileCount") != len(actual_files):
        raise RuntimeError("snapshot fileCount mismatch")
    if manifest.get("bytes") != sum(int(row["bytes"]) for row in actual_files):
        raise RuntimeError("snapshot byte total mismatch")
    return manifest


def restore_canary(snapshot: Path) -> dict[str, Any]:
    """Restore only into an auto-removed temporary directory and verify it there."""
    manifest = verify_snapshot(snapshot)
    expected_dirs, expected_files = normalized_manifest_inventory(manifest)
    with tempfile.TemporaryDirectory(prefix="openclaw-workspace-restore-canary-") as temp_name:
        restored = Path(temp_name) / "snapshot"
        restored.mkdir()
        for relative in sorted(expected_dirs, key=lambda value: (value.count("/"), value)):
            (restored / safe_relative(relative, label="restore directory")).mkdir()
        source_manifest = snapshot / "manifest.json"
        if source_manifest.is_symlink():
            raise RuntimeError("snapshot manifest changed during restore canary")
        secure_copy_regular(source_manifest, restored / "manifest.json")
        for row in expected_files:
            relative = safe_relative(row["path"], label="restore file")
            source = snapshot / relative
            source_stat = source.lstat()
            mode = source_stat.st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or source_stat.st_nlink != 1:
                raise RuntimeError(f"snapshot changed during restore canary: {row['path']}")
            target = restored / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            secure_copy_regular(source, target)
        restored_manifest = verify_snapshot(restored)
        if restored_manifest != manifest:
            raise RuntimeError("restore canary manifest changed during copy")
    return {
        "mode": "restore-canary",
        "ok": True,
        "snapshot": str(snapshot),
        "fileCount": manifest["fileCount"],
        "bytes": manifest["bytes"],
        "temporaryIsolated": True,
    }


def build_snapshot(
    workspace: Path,
    destination_root: Path,
    today: str,
    includes: list[str],
    *,
    root_markdown: bool,
    excludes: list[str],
) -> dict[str, Any]:
    today = validate_today(today)
    workspace = workspace.absolute()
    destination_root = destination_root.absolute()
    normalized_includes = validate_create_inputs(
        workspace,
        destination_root,
        includes,
        excludes,
        root_markdown=root_markdown,
    )
    destination_root.mkdir(parents=True, exist_ok=True)
    final = destination_root / "snapshots" / today
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists() or final.is_symlink():
        raise FileExistsError(f"Snapshot already exists: {final}")
    stage = Path(tempfile.mkdtemp(prefix=f".{today}-stage-", dir=final.parent))
    manifest: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "version": 2,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "workspace": str(workspace),
        "snapshot": str(final),
        "excludes": excludes,
        "sources": [],
    }
    try:
        if root_markdown:
            root_files: list[dict[str, Any]] = []
            root_destination = stage / "root_md"
            root_destination.mkdir()
            for path in sorted(workspace.glob("*.md")):
                if is_excluded(Path(path.name), excludes):
                    continue
                if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                    raise RuntimeError(f"root Markdown source is symlinked or non-regular: {path}")
                target = root_destination / path.name
                secure_copy_regular(path, target)
                root_files.append({
                    "path": path.name,
                    "bytes": target.stat().st_size,
                    "sha256": sha256_file(target),
                })
            manifest["sources"].append({"source": "root_md", "files": root_files})
        for relative in normalized_includes:
            source = workspace / relative
            reject_symlink_components(source)
            if not source.exists():
                raise RuntimeError(f"configured snapshot include is missing: {relative.as_posix()}")
            files = copy_source(source, stage / "workspace" / relative, excludes)
            manifest["sources"].append({"source": relative.as_posix(), "files": files})
        directories, files = inventory_snapshot_tree(stage)
        manifest["directories"] = directories
        manifest["files"] = files
        manifest["fileCount"] = len(files)
        manifest["bytes"] = sum(int(row["bytes"]) for row in files)
        atomic_json(stage / "manifest.json", manifest)
        verify_snapshot(stage)
        os.replace(stage, final)
        verified = verify_snapshot(final)
        restore_canary(final)
        atomic_json(destination_root / "latest.json", {
            "schema": "openclaw-workspace-recovery-latest.v1",
            "snapshot": str(final),
            "createdAt": verified["createdAt"],
            "fileCount": verified["fileCount"],
            "bytes": verified["bytes"],
            "verified": True,
            "restoreCanary": True,
        })
        return verified
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def snapshot_argument(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Path:
    if args.snapshot:
        return Path(args.snapshot).expanduser().absolute()
    if args.destination and args.today:
        return Path(args.destination).expanduser().absolute() / "snapshots" / validate_today(args.today)
    parser.error("verify and restore-canary require --snapshot or both --destination and --today")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create, verify, or restore-canary workspace recovery snapshots.")
    parser.add_argument("operation", nargs="?", choices=("create", "verify", "restore-canary"), default="create")
    parser.add_argument("--workspace")
    parser.add_argument("--destination")
    parser.add_argument("--snapshot")
    parser.add_argument("--today", default=datetime.now().astimezone().date().isoformat())
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--root-markdown", action="store_true")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.operation in {"verify", "restore-canary"}:
        snapshot = snapshot_argument(args, parser)
        result = (
            {"mode": "verify", "ok": True, **{
                key: value for key, value in verify_snapshot(snapshot).items()
                if key in {"schema", "snapshot", "fileCount", "bytes"}
            }}
            if args.operation == "verify"
            else restore_canary(snapshot)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if not args.workspace or not args.destination:
        parser.error("create requires --workspace and --destination")
    workspace = Path(args.workspace).expanduser().absolute()
    destination = Path(args.destination).expanduser().absolute()
    validate_today(args.today)
    excludes = list(DEFAULT_EXCLUDES) + args.exclude
    validate_create_inputs(
        workspace,
        destination,
        args.include,
        excludes,
        root_markdown=args.root_markdown,
    )
    preview = {
        "workspace": str(workspace),
        "destination": str(destination / "snapshots" / args.today),
        "includes": args.include,
        "rootMarkdown": args.root_markdown,
        "excludes": excludes,
    }
    if not args.apply:
        print(json.dumps({"mode": "dry-run", **preview}, ensure_ascii=False, indent=2))
        return 0
    try:
        manifest = build_snapshot(
            workspace,
            destination,
            args.today,
            args.include,
            root_markdown=args.root_markdown,
            excludes=excludes,
        )
    except FileExistsError:
        if not args.skip_existing:
            raise
        snapshot = destination / "snapshots" / validate_today(args.today)
        verified = verify_snapshot(snapshot)
        restore_canary(snapshot)
        print(json.dumps({
            "mode": "skipped",
            "reason": "snapshot_exists_verified",
            "snapshot": str(snapshot),
            "fileCount": verified["fileCount"],
            "bytes": verified["bytes"],
            "restoreCanary": True,
        }, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({
        "mode": "apply",
        "snapshot": manifest["snapshot"],
        "fileCount": manifest["fileCount"],
        "bytes": manifest["bytes"],
        "verified": True,
        "restoreCanary": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
