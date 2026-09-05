#!/usr/bin/env python3
"""Transactional installer/upgrader for the complete Discord backup product."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backup_paths import BackupLayout, build_layout, resolved, validate_layout


DEFAULT_CONFIG: dict[str, Any] = {
    "guildId": "CHANGE_ME",
    "statePath": "memory/channel_backup_summary_state.json",
    "queuePath": "memory/channel_backup_backlog_queue.json",
    "reportChannel": "discord:channel:CHANGE_ME",
    "agentId": "main",
    "timezone": "Asia/Taipei",
    "receiptDir": "memory/openclaw_discord_backup_health",
    "limits": {
        "dailyEntryLimit": 6,
        "dailyMessageLimitPerEntry": 60,
        "dailyLookbackLimit": 10,
        "dailyFreshnessDays": 2,
        "backlogEntryLimit": 4,
        "backlogBatchLimit": 12,
        "backlogPageLimit": 100,
        "auditProbeLimit": 1,
    },
}
GUILD_RE = re.compile(r"^[0-9]{6,32}$")
REPORT_RE = re.compile(r"^(?:discord:)?(?:channel|user):[0-9]{6,32}$")


class InstallError(RuntimeError):
    pass


class InstallRollbackIncomplete(InstallError):
    """Cron state is uncertain; compatible runtime/config must be preserved."""


@dataclass
class FileBefore:
    path: Path
    existed: bool
    data: bytes | None
    mode: int | None


@dataclass
class SkillSwap:
    target: Path
    old: Path | None
    changed: bool


def normalized_file_mode(path: Path) -> int:
    """Return the release identity for one regular file's execute bits."""
    return 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644


def canonical_json(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def reject_symlink_components(path: Path, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise InstallError(f"{label} contains a symlinked path component")


def secure_parent(path: Path) -> None:
    reject_symlink_components(path, "managed path")
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise InstallError("managed path parent is unsafe")
    for item in reversed(missing):
        item.mkdir(mode=0o700)


def atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    secure_parent(path.parent)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def atomic_if_changed(path: Path, data: bytes, mode: int = 0o600) -> bool:
    if path.exists() and not path.is_symlink() and path.is_file() and path.read_bytes() == data:
        return False
    atomic_bytes(path, data, mode)
    return True


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"existing JSON cannot be read safely: {path.name}") from exc
    if not isinstance(data, dict):
        raise InstallError(f"existing JSON must contain an object: {path.name}")
    return data


def snapshot_file(path: Path) -> FileBefore:
    if path.is_symlink():
        raise InstallError(f"managed file must not be a symlink: {path.name}")
    if not path.exists():
        return FileBefore(path, False, None, None)
    if not path.is_file():
        raise InstallError(f"managed file must be regular: {path.name}")
    return FileBefore(path, True, path.read_bytes(), path.stat().st_mode & 0o777)


def restore_file(before: FileBefore) -> None:
    if before.existed:
        atomic_bytes(before.path, before.data or b"", before.mode or 0o600)
    else:
        try:
            before.path.unlink()
        except FileNotFoundError:
            pass


def workspace_path(workspace: Path, value: str, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = Path(os.path.abspath(candidate))
    reject_symlink_components(candidate, label)
    candidate = candidate.resolve(strict=False)
    if candidate != workspace and workspace not in candidate.parents:
        raise InstallError(f"{label} must stay inside the OpenClaw workspace")
    reject_symlink_components(candidate, label)
    return candidate


def _open_directory_beneath(root_fd: int, parts: tuple[str, ...], *, label: str) -> int:
    """Open an in-root directory without following any path component symlink."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            if part in {"", ".", ".."} or "/" in part or "\x00" in part:
                raise InstallError(f"{label} contains an unsafe path component")
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise InstallError(f"{label} is not a stable real directory") from exc
            os.close(current_fd)
            current_fd = next_fd
            info = os.fstat(current_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise InstallError(f"{label} is not a real directory")
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _same_entry(before: os.stat_result, after: os.stat_result) -> bool:
    """Compare enough metadata to reject replacement during one integrity read."""
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        return False
    return (
        getattr(before, "st_ctime_ns", int(before.st_ctime * 1_000_000_000))
        == getattr(after, "st_ctime_ns", int(after.st_ctime * 1_000_000_000))
    )


def _hash_regular_file_at(parent_fd: int, name: str, expected: os.stat_result) -> bytes:
    """Read one regular file through the already-validated parent descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise InstallError("raw root changed during integrity read") from exc
    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or not _same_entry(expected, opened):
            raise InstallError("raw root changed during integrity read")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if not _same_entry(opened, os.fstat(file_fd)):
            raise InstallError("raw root changed during integrity read")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_entry(opened, current):
            raise InstallError("raw root changed during integrity read")
        return digest.digest()
    finally:
        os.close(file_fd)


def _hash_tree_directory(
    digest: Any,
    *,
    root: Path,
    root_fd: int,
    directory_fd: int,
    prefix: Path,
    alias_targets: dict[tuple[str, ...], os.stat_result],
    visited_directories: dict[tuple[str, ...], os.stat_result],
) -> None:
    """Hash one directory tree using only descriptors rooted at ``root_fd``."""
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise InstallError("raw root changed during integrity read") from exc
    for name in names:
        if name in {"", ".", ".."} or "/" in name or "\x00" in name:
            raise InstallError("raw root contains an unsafe entry name")
        relative_path = prefix / name
        relative = relative_path.as_posix().encode("utf-8")
        try:
            entry_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise InstallError("raw root changed during integrity read") from exc
        if stat.S_ISLNK(entry_info.st_mode):
            if entry_info.st_uid != os.getuid() or entry_info.st_nlink != 1:
                raise InstallError("raw root contains an unsafe symlink")
            try:
                link_target = os.readlink(name, dir_fd=directory_fd)
            except OSError as exc:
                raise InstallError("raw root contains a broken symlink") from exc
            lexical_target = Path(link_target)
            if not lexical_target.is_absolute():
                lexical_target = root / prefix / lexical_target
            canonical_target = Path(os.path.abspath(lexical_target))
            if canonical_target == root or root not in canonical_target.parents:
                raise InstallError("raw root contains an external symlink")
            target_relative_path = canonical_target.relative_to(root)
            target_fd = _open_directory_beneath(
                root_fd, target_relative_path.parts, label="raw alias target"
            )
            try:
                target_info = os.fstat(target_fd)
                if target_info.st_uid != os.getuid():
                    raise InstallError("raw root contains an unsafe symlink")
                target_key = target_relative_path.parts
                previous_target = alias_targets.get(target_key)
                visited_target = visited_directories.get(target_key)
                if previous_target is not None and not _same_entry(previous_target, target_info):
                    raise InstallError("raw alias target changed during integrity read")
                if visited_target is not None and not _same_entry(visited_target, target_info):
                    raise InstallError("raw alias target changed during integrity read")
                alias_targets[target_key] = target_info
                link_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_entry(entry_info, link_after):
                    raise InstallError("raw root changed during integrity read")
                if os.readlink(name, dir_fd=directory_fd) != link_target:
                    raise InstallError("raw root changed during integrity read")
            finally:
                os.close(target_fd)
            target_relative = target_relative_path.as_posix().encode("utf-8")
            digest.update(
                b"L\0" + relative + b"\0" + os.fsencode(link_target) + b"\0"
                + target_relative + b"\0"
                + f"{entry_info.st_dev}:{entry_info.st_ino}:{target_info.st_dev}:{target_info.st_ino}".encode("ascii")
                + b"\0"
            )
        elif stat.S_ISDIR(entry_info.st_mode):
            child_fd = _open_directory_beneath(directory_fd, (name,), label="raw directory")
            try:
                opened = os.fstat(child_fd)
                if not _same_entry(entry_info, opened):
                    raise InstallError("raw root changed during integrity read")
                directory_key = relative_path.parts
                expected_target = alias_targets.get(directory_key)
                if expected_target is not None and not _same_entry(expected_target, opened):
                    raise InstallError("raw alias target changed during integrity read")
                visited_directories[directory_key] = opened
                digest.update(b"D\0" + relative + b"\0")
                _hash_tree_directory(
                    digest,
                    root=root,
                    root_fd=root_fd,
                    directory_fd=child_fd,
                    prefix=relative_path,
                    alias_targets=alias_targets,
                    visited_directories=visited_directories,
                )
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_entry(opened, current):
                    raise InstallError("raw root changed during integrity read")
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(entry_info.st_mode):
            digest.update(
                b"F\0" + relative + b"\0"
                + _hash_regular_file_at(directory_fd, name, entry_info)
            )
        else:
            raise InstallError("raw root contains a non-regular entry")
    try:
        if sorted(os.listdir(directory_fd)) != names:
            raise InstallError("raw root changed during integrity read")
    except OSError as exc:
        raise InstallError("raw root changed during integrity read") from exc


def tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return digest.hexdigest()
    if path.is_symlink() or not path.is_dir():
        raise InstallError("raw root is unsafe")
    root = Path(os.path.abspath(path))
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(root, root_flags)
    except OSError as exc:
        raise InstallError("raw root is unsafe") from exc
    try:
        root_info = os.fstat(root_fd)
        alias_targets: dict[tuple[str, ...], os.stat_result] = {}
        visited_directories: dict[tuple[str, ...], os.stat_result] = {}
        _hash_tree_directory(
            digest,
            root=root,
            root_fd=root_fd,
            directory_fd=root_fd,
            prefix=Path(),
            alias_targets=alias_targets,
            visited_directories=visited_directories,
        )
        if set(alias_targets) - set(visited_directories):
            raise InstallError("raw alias target was not traversed")
        for target_key, expected in alias_targets.items():
            if not _same_entry(expected, visited_directories[target_key]):
                raise InstallError("raw alias target changed during integrity read")
        try:
            current_root = os.stat(root, follow_symlinks=False)
        except OSError as exc:
            raise InstallError("raw root changed during integrity read") from exc
        if not _same_entry(root_info, current_root):
            raise InstallError("raw root changed during integrity read")
    finally:
        os.close(root_fd)
    return digest.hexdigest()


def skill_tree_hash(path: Path) -> str:
    """Hash distributable Skill content while ignoring generated local debris."""
    digest = hashlib.sha256()
    if not path.exists():
        return digest.hexdigest()
    if path.is_symlink() or not path.is_dir():
        raise InstallError("Skill tree is unsafe")
    for item in sorted(path.rglob("*"), key=lambda row: row.relative_to(path).as_posix()):
        relative = item.relative_to(path)
        if any(part == "__pycache__" for part in relative.parts) or item.name.endswith(".pyc") or item.name == ".DS_Store":
            continue
        encoded = relative.as_posix().encode("utf-8")
        if item.is_symlink():
            raise InstallError("Skill tree contains a symlink")
        if item.is_dir():
            digest.update(b"D\0" + encoded + b"\0")
        elif item.is_file():
            mode = f"{normalized_file_mode(item):04o}".encode("ascii")
            digest.update(b"F\0" + encoded + b"\0" + mode + b"\0" + hashlib.sha256(item.read_bytes()).digest())
        else:
            raise InstallError("Skill tree contains a non-regular entry")
    return digest.hexdigest()


def trusted_executable(value: str, *, label: str) -> str:
    candidate = Path(value).expanduser() if Path(value).is_absolute() else Path(shutil.which(value) or "")
    if not str(candidate):
        raise InstallError(f"{label} executable was not found")
    try:
        resolved_path = candidate.resolve(strict=True)
        info = resolved_path.stat()
    except OSError as exc:
        raise InstallError(f"{label} executable is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.getuid()}
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(resolved_path, os.X_OK)
    ):
        raise InstallError(f"{label} executable is unsafe")
    return str(resolved_path)


def acquire_install_lock(path: Path) -> int:
    """Acquire one existing product mutex without following a lock symlink."""
    secure_parent(path.parent)
    reject_symlink_components(path.parent, "backup lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise InstallError("backup lock file is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BlockingIOError as exc:
        os.close(descriptor)
        raise InstallError("a backup job is still running; retry the upgrade after it finishes") from exc
    except Exception:
        os.close(descriptor)
        raise


def release_install_locks(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def validate_identity(guild_id: str, report_to: str, agent: str, timezone_name: str) -> None:
    if not GUILD_RE.fullmatch(guild_id):
        raise InstallError("a numeric --guild-id is required for a ready install")
    if not REPORT_RE.fullmatch(report_to):
        raise InstallError("an explicit channel:ID or user:ID --report-to is required")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", agent):
        raise InstallError("a valid --agent is required")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise InstallError("--timezone must be a valid IANA timezone") from exc


def choose_value(existing: dict[str, Any] | None, key: str, cli: str | None, placeholder: str | None = None) -> str:
    current = str((existing or {}).get(key) or "")
    if cli is not None:
        if current and current != placeholder and current != cli:
            raise InstallError(f"existing {key} differs from the requested value; explicit migration is required")
        return cli
    return current


def stage_and_swap_skill(source: Path, target: Path) -> SkillSwap:
    if source.resolve() == target.resolve():
        check = subprocess.run([sys.executable, str(source / "scripts/post_run_check.py")], text=True, capture_output=True, check=False)
        if check.returncode != 0:
            raise InstallError("current installed Skill failed its self-check")
        return SkillSwap(target, None, False)
    if target.exists() and target.is_dir() and not target.is_symlink() and skill_tree_hash(source) == skill_tree_hash(target):
        check = subprocess.run([sys.executable, str(target / "scripts/post_run_check.py")], text=True, capture_output=True, check=False)
        if check.returncode != 0:
            raise InstallError("installed Skill failed its self-check")
        return SkillSwap(target, None, False)
    secure_parent(target.parent)
    token = uuid.uuid4().hex
    staged = target.parent / f".{target.name}.stage-{token}"
    old = target.parent / f".{target.name}.rollback-{token}"
    if staged.exists() or old.exists():
        raise InstallError("transaction staging path already exists")
    shutil.copytree(
        source, staged,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        symlinks=True,
    )
    if any(item.is_symlink() for item in staged.rglob("*")):
        shutil.rmtree(staged)
        raise InstallError("Skill package contains a symlink")
    if skill_tree_hash(source) != skill_tree_hash(staged):
        shutil.rmtree(staged)
        raise InstallError("staged Skill differs from source")
    check = subprocess.run([sys.executable, str(staged / "scripts/post_run_check.py")], text=True, capture_output=True, check=False)
    if check.returncode != 0:
        shutil.rmtree(staged)
        raise InstallError("staged Skill failed its installed-layout self-check")
    old_path: Path | None = None
    try:
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise InstallError("installed Skill target is unsafe")
            target.rename(old)
            old_path = old
        staged.rename(target)
    except Exception:
        if old_path and old.exists() and not target.exists():
            old.rename(target)
        if staged.exists():
            shutil.rmtree(staged)
        raise
    return SkillSwap(target, old_path, True)


def rollback_skill(swap: SkillSwap) -> None:
    if not swap.changed:
        return
    failed = swap.target.parent / f".{swap.target.name}.failed-{uuid.uuid4().hex}"
    if swap.target.exists():
        swap.target.rename(failed)
    if swap.old and swap.old.exists():
        swap.old.rename(swap.target)
    if failed.exists():
        shutil.rmtree(failed)


def finalize_skill(swap: SkillSwap) -> None:
    if swap.old and swap.old.exists():
        shutil.rmtree(swap.old)


def write_not_due_baselines(
    workspace: Path,
    config: dict[str, Any],
    file_snapshots: list[FileBefore],
) -> None:
    receipt_dir = workspace_path(workspace, str(config["receiptDir"]), "receipt directory")
    checked_at = datetime.now(ZoneInfo(str(config["timezone"]))).isoformat()
    guild_id = str(config["guildId"])
    for component, cadence in (
        ("weekly-inventory", "每週"),
        ("weekly-raw", "每週"),
        ("workspace-snapshot", "每月"),
    ):
        path = receipt_dir / "components" / f"{component}.json"
        if os.path.lexists(path):
            if path.is_symlink() or not path.is_file():
                raise InstallError("scheduled baseline receipt path is unsafe")
            continue
        file_snapshots.append(snapshot_file(path))
        payload = {
            "schema": "backup-health-component.v1",
            "producer": "openclaw-discord-server-backup/installer.v1",
            "declarationKey": f"openclaw-discord-server-backup:{guild_id}:{component}:v1",
            "component": component,
            "status": "pending",
            "checkedAt": checked_at,
            "summary": f"尚未到首次{cadence}排程",
            "checks": [{"key": "first_cycle", "status": "pending", "summary": "排程已安裝，首次週期尚未到"}],
            "metrics": {"baselineNotDue": True},
            "anomalies": [],
            "pending": [],
        }
        atomic_bytes(path, canonical_json(payload))


def validate_snapshot_includes(workspace: Path, config: dict[str, Any], target_skill: Path) -> None:
    settings = config.get("workspaceSnapshot")
    includes = settings.get("includes") if isinstance(settings, dict) else None
    if not isinstance(includes, list) or not includes:
        raise InstallError("workspaceSnapshot.includes must be a non-empty array")
    normalized: set[str] = set()
    normalized_paths: list[Path] = []
    for value in includes:
        if not isinstance(value, str) or not value.strip():
            raise InstallError("workspace snapshot include must be a non-empty relative path")
        path_value = Path(value)
        if path_value.is_absolute() or any(part in {"", ".", ".."} for part in path_value.parts):
            raise InstallError("workspace snapshot include must be a safe relative path")
        key = path_value.as_posix()
        if key in normalized:
            raise InstallError("workspace snapshot includes contain a duplicate")
        for previous in normalized_paths:
            if path_value.is_relative_to(previous) or previous.is_relative_to(path_value):
                raise InstallError("workspace snapshot includes must not overlap")
        normalized.add(key)
        normalized_paths.append(path_value)
        candidate = Path(os.path.abspath(workspace / path_value))
        reject_symlink_components(candidate, "workspace snapshot include")
        future_managed_dir = key == "memory" or (key == "skills" and target_skill.parent == workspace / "skills")
        if not future_managed_dir and (not candidate.exists() or not candidate.is_dir()):
            raise InstallError(f"workspace snapshot include does not exist: {key}")


def run_cron_manager(
    target_skill: Path,
    *,
    workspace: Path,
    config_path: Path,
    layout: Any,
    config: dict[str, Any],
    openclaw_bin: str,
    adoption_map: Path | None,
    prepared_adoption_receipt: Path | None,
    account_id: str | None,
    skip_canary: bool,
) -> dict[str, Any]:
    receipt_dir = workspace_path(workspace, str(config["receiptDir"]), "receipt directory")
    command = [
        sys.executable, str(target_skill / "scripts/manage_cron_topology.py"), "apply",
        "--workspace", str(workspace), "--skill-dir", str(target_skill),
        "--config", str(config_path), "--backup-root", str(layout.discord_root),
        "--guild-id", str(config["guildId"]), "--report-to", str(config["reportChannel"]),
        "--agent", str(config["agentId"]), "--timezone", str(config["timezone"]),
        "--receipt-dir", str(receipt_dir), "--openclaw-bin", openclaw_bin,
        "--python-executable", sys.executable,
    ]
    if account_id:
        command.extend(["--account-id", account_id])
    if adoption_map:
        command.extend(["--adoption-map", str(adoption_map)])
    if prepared_adoption_receipt:
        command.extend(["--prepared-adoption-receipt", str(prepared_adoption_receipt)])
    if skip_canary:
        command.append("--skip-canary")
    proc = subprocess.run(command, cwd=workspace, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        if "rollback was incomplete" in (proc.stderr + proc.stdout).lower():
            raise InstallRollbackIncomplete("owned cron topology reconciliation failed and rollback is incomplete")
        raise InstallError("owned cron topology reconciliation failed; prior enabled state was restored")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError("cron manager returned invalid JSON") from exc
    if result.get("status") != "ready":
        raise InstallError("cron manager did not return ready")
    return result


def prepare_cron_quiescence(
    source_skill: Path,
    *,
    workspace: Path,
    config_path: Path,
    layout: Any,
    config: dict[str, Any],
    openclaw_bin: str,
    adoption_map: Path | None,
    prepared_adoption_receipt: Path | None,
    account_id: str | None,
) -> dict[str, Any]:
    receipt_dir = workspace_path(workspace, str(config["receiptDir"]), "receipt directory")
    command = [
        sys.executable, str(source_skill / "scripts/manage_cron_topology.py"), "prepare-quiescence",
        "--workspace", str(workspace), "--skill-dir", str(source_skill),
        "--config", str(config_path), "--backup-root", str(layout.discord_root),
        "--guild-id", str(config["guildId"]), "--report-to", str(config["reportChannel"]),
        "--agent", str(config["agentId"]), "--timezone", str(config["timezone"]),
        "--receipt-dir", str(receipt_dir), "--openclaw-bin", openclaw_bin,
        "--python-executable", sys.executable,
    ]
    if adoption_map:
        command.extend(["--adoption-map", str(adoption_map)])
    if prepared_adoption_receipt:
        command.extend(["--prepared-adoption-receipt", str(prepared_adoption_receipt)])
    if account_id:
        command.extend(["--account-id", account_id])
    proc = subprocess.run(command, cwd=workspace, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        if "rollback was incomplete" in (proc.stderr + proc.stdout).lower():
            raise InstallRollbackIncomplete("backup quiescence failed and rollback is incomplete")
        raise InstallError("backup jobs could not be safely quiesced before installation")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError("quiescence manager returned invalid JSON") from exc
    if result.get("status") not in {"quiescence_prepared", "no_quiescence_needed"}:
        raise InstallError("quiescence manager did not return a verified result")
    if result.get("status") == "quiescence_prepared" and not result.get("transaction"):
        raise InstallError("quiescence manager did not return its rollback receipt")
    return result


def rollback_cron_transaction(target_skill: Path, transaction: Path, openclaw_bin: str) -> None:
    proc = subprocess.run([
        sys.executable, str(target_skill / "scripts/manage_cron_topology.py"),
        "rollback-receipt", "--receipt", str(transaction), "--openclaw-bin", openclaw_bin,
    ], text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise InstallError("post-install failure occurred and committed cron rollback did not complete")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Install or transactionally upgrade OpenClaw Discord backup.")
    ap.add_argument("--workspace", default="~/.openclaw/workspace")
    ap.add_argument("--skill-dir")
    ap.add_argument("--config", default="memory/openclaw_discord_backup_config.json")
    ap.add_argument("--server-name")
    ap.add_argument("--backup-root")
    ap.add_argument("--desktop-dir", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--guild-id")
    ap.add_argument("--report-to")
    ap.add_argument("--agent")
    ap.add_argument("--timezone")
    ap.add_argument("--account-id")
    ap.add_argument("--openclaw-bin", default="openclaw")
    ap.add_argument("--adoption-map")
    ap.add_argument("--qwen-receipt", help="Explicit receipt path from the owned Qwen-local installer")
    ap.add_argument("--offline-scaffold", action="store_true")
    ap.add_argument("--force", action="store_true", help="Deprecated compatibility flag; upgrades are already convergent")
    ap.add_argument("--skip-canary", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--fault-after-cron", action="store_true", help=argparse.SUPPRESS)
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    workspace_lexical = Path(os.path.abspath(Path(args.workspace).expanduser()))
    try:
        reject_symlink_components(workspace_lexical, "workspace")
    except InstallError as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}), file=sys.stderr)
        return 2
    workspace = workspace_lexical.resolve(strict=False)
    if not workspace.exists() or not workspace.is_dir() or workspace.is_symlink():
        print(json.dumps({"status": "BLOCKED", "error": "workspace is missing or unsafe"}), file=sys.stderr)
        return 2
    source_skill = Path(__file__).resolve().parents[1]
    target_lexical = Path(os.path.abspath(Path(args.skill_dir).expanduser())) if args.skill_dir else workspace / "skills" / source_skill.name
    try:
        reject_symlink_components(target_lexical, "skill target")
    except InstallError as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}), file=sys.stderr)
        return 2
    target_skill = target_lexical.resolve(strict=False)
    swap: SkillSwap | None = None
    file_snapshots: list[FileBefore] = []
    cron_transaction: Path | None = None
    quiescence_transaction: Path | None = None
    prepared_adoption_receipt: Path | None = None
    install_locks: list[int] = []
    try:
        bound_openclaw = args.openclaw_bin if args.offline_scaffold else trusted_executable(args.openclaw_bin, label="OpenClaw")
        config_path = workspace_path(workspace, args.config, "config path")
        existing_config = read_json(config_path) if config_path.exists() else None
        configured_root = str((existing_config or {}).get("backupRoot") or "")
        if configured_root and args.backup_root is None:
            # Existing installations may use the historical `頻道紀錄` child.
            # Preserve that exact data root; folder renaming is a separate migration.
            discord_root = resolved(Path(configured_root))
            layout = BackupLayout(
                backup_root=discord_root.parent,
                discord_root=discord_root,
                core_root=discord_root.parent / "核心文件",
            )
        else:
            layout = build_layout(server_name=args.server_name, backup_root=args.backup_root, desktop_dir=args.desktop_dir)
        validate_layout(layout, workspace)
        if configured_root and resolved(Path(configured_root)) != layout.discord_root:
            raise InstallError("existing backup root differs; explicit migration is required")

        state_relative = str((existing_config or {}).get("statePath") or DEFAULT_CONFIG["statePath"])
        queue_relative = str((existing_config or {}).get("queuePath") or DEFAULT_CONFIG["queuePath"])
        state_path = workspace_path(workspace, state_relative, "state path")
        queue_path = workspace_path(workspace, queue_relative, "queue path")
        existing_state = read_json(state_path) if state_path.exists() else None
        if existing_state and resolved(Path(str(existing_state.get("rootPath") or ""))) != layout.discord_root:
            raise InstallError("existing state root differs; explicit migration is required")
        file_snapshots = [snapshot_file(path) for path in (config_path, state_path, queue_path)]
        config = json.loads(json.dumps(existing_config or DEFAULT_CONFIG))
        config["backupRoot"] = str(layout.discord_root)
        config["statePath"] = state_relative
        config["queuePath"] = queue_relative
        guild_id = choose_value(existing_config, "guildId", args.guild_id, "CHANGE_ME")
        report_to = choose_value(existing_config, "reportChannel", args.report_to, "discord:channel:CHANGE_ME")
        agent = choose_value(existing_config, "agentId", args.agent, None) or "main"
        timezone_name = choose_value(existing_config, "timezone", args.timezone, None) or "Asia/Taipei"
        if not args.offline_scaffold:
            validate_identity(guild_id, report_to, agent, timezone_name)
        config.update({"guildId": guild_id or "CHANGE_ME", "reportChannel": report_to or "discord:channel:CHANGE_ME", "agentId": agent, "timezone": timezone_name})
        config.setdefault("receiptDir", DEFAULT_CONFIG["receiptDir"])
        # Executable paths are reviewed installer inputs and are bound into the
        # owned cron argv. Never persist or trust an executable redirect from
        # customer-editable runtime config.
        config.pop("openclawExecutable", None)
        config.pop("pythonExecutable", None)
        if args.account_id:
            config["accountId"] = args.account_id
        cron_settings = config.get("cron") if isinstance(config.get("cron"), dict) else {}
        configured_adoption = cron_settings.get("adoptionMap")
        if args.adoption_map:
            adoption_path = workspace_path(workspace, args.adoption_map, "adoption map")
            if configured_adoption:
                prior_adoption = workspace_path(workspace, str(configured_adoption), "adoption map")
                if prior_adoption != adoption_path:
                    raise InstallError("existing adoption map differs; explicit recovery is required")
            config.setdefault("cron", {})["adoptionMap"] = str(adoption_path.relative_to(workspace))
        elif configured_adoption:
            adoption_path = workspace_path(workspace, str(configured_adoption), "adoption map")
        else:
            adoption_path = None
        configured_prepared = cron_settings.get("preparedAdoptionReceipt")
        if configured_prepared:
            prepared_adoption_receipt = workspace_path(
                workspace, str(configured_prepared), "prepared adoption receipt"
            )
            if adoption_path is None:
                raise InstallError("prepared adoption receipt requires its configured adoption map")
        if args.qwen_receipt:
            qwen_lexical = Path(args.qwen_receipt).expanduser()
            if not qwen_lexical.is_absolute():
                raise InstallError("Qwen receipt path must be absolute")
            qwen_lexical = Path(os.path.abspath(qwen_lexical))
            reject_symlink_components(qwen_lexical, "Qwen receipt path")
            qwen_path = qwen_lexical.resolve(strict=False)
            config.setdefault("health", {})["qwenReceiptPath"] = str(qwen_path)
        elif isinstance(config.get("health"), dict) and config["health"].get("qwenReceiptPath"):
            qwen_lexical = Path(str(config["health"]["qwenReceiptPath"])).expanduser()
            if not qwen_lexical.is_absolute():
                raise InstallError("configured Qwen receipt path must be absolute")
            reject_symlink_components(Path(os.path.abspath(qwen_lexical)), "Qwen receipt path")
        if not isinstance(config.get("workspaceSnapshot"), dict):
            candidates = [
                name for name in ("memory", "scripts", "skills", "hooks", "records", "reports")
                if (workspace / name).is_dir() and not (workspace / name).is_symlink()
            ]
            if target_skill.parent == workspace / "skills" and "skills" not in candidates:
                candidates.append("skills")
            if "memory" not in candidates:
                candidates.insert(0, "memory")
            config["workspaceSnapshot"] = {"includes": candidates}
        validate_snapshot_includes(workspace, config, target_skill)

        state = existing_state or {
            "version": 3, "schema": "channel-backup-state-v3", "guildId": config["guildId"],
            "rootPath": str(layout.discord_root), "queuePath": queue_relative, "entries": {},
        }
        if str(state.get("guildId") or "") in {"", "CHANGE_ME"}:
            state["guildId"] = config["guildId"]
        elif not args.offline_scaffold and str(state["guildId"]) != config["guildId"]:
            raise InstallError("existing state guild ID differs; explicit migration is required")

        receipt_dir = workspace_path(workspace, str(config["receiptDir"]), "receipt directory")
        baseline_paths = [
            receipt_dir / "components" / f"{component}.json"
            for component in ("weekly-inventory", "weekly-raw", "workspace-snapshot")
        ]
        skill_change_expected = (
            source_skill.resolve() != target_skill.resolve()
            and (
                not target_skill.exists()
                or target_skill.is_symlink()
                or not target_skill.is_dir()
                or skill_tree_hash(source_skill) != skill_tree_hash(target_skill)
            )
        )
        config_change_expected = (
            not config_path.exists() or config_path.read_bytes() != canonical_json(config)
        )
        state_change_expected = (
            not state_path.exists() or state_path.read_bytes() != canonical_json(state)
        )
        filesystem_change_expected = any((
            skill_change_expected,
            config_change_expected,
            state_change_expected,
            not queue_path.exists(),
            not layout.backup_root.exists(),
            not layout.discord_root.exists(),
            (not args.offline_scaffold and any(not os.path.lexists(path) for path in baseline_paths)),
        ))

        # Any ready-mode upgrade that will touch files first stops every exact
        # owned/adopted job and records how to restore it. This happens before
        # layout, config, state, baseline receipt, or Skill mutation. A prepared
        # receipt from a prior successful adoption remains the authorization for
        # an already-disabled legacy job on idempotent reruns.
        if not args.offline_scaffold and existing_config is not None and filesystem_change_expected:
            quiescence_result = prepare_cron_quiescence(
                source_skill, workspace=workspace, config_path=config_path,
                layout=layout, config=config, openclaw_bin=bound_openclaw,
                adoption_map=adoption_path,
                prepared_adoption_receipt=prepared_adoption_receipt,
                account_id=args.account_id or str(config.get("accountId") or "") or None,
            )
            if quiescence_result.get("transaction"):
                quiescence_transaction = Path(str(quiescence_result["transaction"]))
                if adoption_path is not None and prepared_adoption_receipt is None:
                    prepared_adoption_receipt = quiescence_transaction
                    config.setdefault("cron", {})["preparedAdoptionReceipt"] = str(
                        prepared_adoption_receipt.relative_to(workspace)
                    )
        elif adoption_path is not None and existing_config is None:
            raise InstallError("legacy adoption requires an existing installed config")

        if not args.offline_scaffold and existing_config is not None:
            install_locks.append(acquire_install_lock(state_path.parent / ".channel_backup.lock"))
            if layout.core_root.exists() and layout.core_root.is_dir() and not layout.core_root.is_symlink():
                install_locks.append(acquire_install_lock(layout.core_root / ".core-backup.lock"))

        raw_hash_before = tree_hash(layout.discord_root) if layout.discord_root.exists() else None

        layout.backup_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        layout.discord_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        atomic_if_changed(config_path, canonical_json(config))
        atomic_if_changed(state_path, canonical_json(state))
        if not queue_path.exists():
            atomic_bytes(queue_path, canonical_json({"version": 1, "items": []}))

        if not args.offline_scaffold:
            write_not_due_baselines(workspace, config, file_snapshots)
            topology_receipt = workspace_path(workspace, str(config["receiptDir"]), "receipt directory") / "components/cron-topology.json"
            if all(before.path != topology_receipt for before in file_snapshots):
                file_snapshots.append(snapshot_file(topology_receipt))

        swap = stage_and_swap_skill(source_skill, target_skill)
        if args.offline_scaffold:
            finalize_skill(swap)
            release_install_locks(install_locks)
            install_locks = []
            print(json.dumps({
                "status": "PARTIAL_MANUAL_ACTION", "installedSkill": str(target_skill),
                "backupRoot": str(layout.backup_root), "discordDataRoot": str(layout.discord_root),
                "coreDataRoot": str(layout.core_root),
                "config": str(config_path), "state": str(state_path), "queue": str(queue_path),
                "pending": ["Supply guild/report/agent and rerun without --offline-scaffold to reconcile cron jobs"],
            }, ensure_ascii=False, indent=2))
            return 0

        cron_result = run_cron_manager(
            target_skill, workspace=workspace, config_path=config_path, layout=layout, config=config,
            openclaw_bin=bound_openclaw, adoption_map=adoption_path,
            prepared_adoption_receipt=prepared_adoption_receipt,
            account_id=args.account_id or str(config.get("accountId") or "") or None,
            skip_canary=args.skip_canary,
        )
        if cron_result.get("transaction"):
            cron_transaction = Path(str(cron_result["transaction"]))
        if args.fault_after_cron:
            raise InstallError("injected post-cron installation failure")
        if raw_hash_before is not None and tree_hash(layout.discord_root) != raw_hash_before:
            raise InstallError("customer raw archive changed during installation")
        try:
            finalize_skill(swap)
        except OSError:
            # The new Skill and cron contract are already verified. A leftover
            # private rollback directory is cleanup debt, not a reason to undo
            # a healthy install.
            pass
        release_install_locks(install_locks)
        install_locks = []
        print(json.dumps({
            "status": "READY", "installedSkill": str(target_skill),
            "backupRoot": str(layout.backup_root), "discordDataRoot": str(layout.discord_root),
            "coreDataRoot": str(layout.core_root), "config": str(config_path),
            "state": str(state_path), "queue": str(queue_path), "cron": cron_result,
        }, ensure_ascii=False, indent=2))
        return 0
    except (InstallError, ValueError, OSError, subprocess.SubprocessError) as exc:
        rollback_problems: list[str] = []
        preserve_runtime = isinstance(exc, InstallRollbackIncomplete)
        filesystem_rollback_failed = False
        if cron_transaction is not None and swap is not None:
            try:
                rollback_cron_transaction(swap.target, cron_transaction, bound_openclaw)
            except Exception as rollback_exc:
                rollback_problems.append(str(rollback_exc))
                preserve_runtime = True
        if quiescence_transaction is not None and not preserve_runtime:
            try:
                manager_skill = swap.target if swap is not None and swap.target.exists() else source_skill
                rollback_cron_transaction(manager_skill, quiescence_transaction, bound_openclaw)
            except Exception as rollback_exc:
                rollback_problems.append(str(rollback_exc))
                preserve_runtime = True
        if not preserve_runtime:
            if swap is not None:
                try:
                    rollback_skill(swap)
                except Exception as rollback_exc:
                    rollback_problems.append(f"restore-skill:{type(rollback_exc).__name__}")
                    filesystem_rollback_failed = True
                    preserve_runtime = True
            for before in reversed(file_snapshots):
                try:
                    restore_file(before)
                except Exception as rollback_exc:
                    rollback_problems.append(
                        f"restore-file:{before.path.name}:{type(rollback_exc).__name__}"
                    )
                    filesystem_rollback_failed = True
                    preserve_runtime = True
        if install_locks:
            try:
                release_install_locks(install_locks)
            except Exception:
                pass
            install_locks = []
        payload = {"status": "BLOCKED", "error": str(exc)}
        if preserve_runtime:
            payload["rollback"] = "incomplete"
            payload["runtimePreserved"] = not filesystem_rollback_failed
            payload["runtimeState"] = "uncertain" if filesystem_rollback_failed or rollback_problems else "compatible_files_preserved"
            payload["recovery"] = (
                "filesystem rollback is incomplete; inspect transaction receipts before retrying"
                if filesystem_rollback_failed
                else "compatible installed files were preserved for receipt-backed manual cron recovery"
            )
        if rollback_problems:
            payload["rollbackErrors"] = rollback_problems
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
