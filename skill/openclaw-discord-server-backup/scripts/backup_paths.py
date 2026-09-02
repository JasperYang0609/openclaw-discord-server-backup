#!/usr/bin/env python3
from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path


BACKUP_SUFFIX = "資料備份"
DISCORD_DIRNAME = "Discord資料"
CORE_DIRNAME = "核心文件"


@dataclass(frozen=True)
class BackupLayout:
    backup_root: Path
    discord_root: Path
    core_root: Path


def resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def paths_overlap(first: Path, second: Path) -> bool:
    left = resolved(first)
    right = resolved(second)
    return left == right or left in right.parents or right in left.parents


def validate_server_name(value: str) -> str:
    name = value.strip()
    if not name or name in {".", ".."}:
        raise ValueError("Discord server name must not be empty, '.' or '..'")
    if any(character in {"/", "\\", "\x00"} for character in name):
        raise ValueError("Discord server name must not contain path separators or NUL bytes")
    if any(unicodedata.category(character) == "Cc" for character in name):
        raise ValueError("Discord server name must not contain control characters")
    return name


def build_layout(
    *,
    server_name: str | None,
    backup_root: str | None,
    desktop_dir: str | Path | None = None,
) -> BackupLayout:
    if backup_root:
        root = resolved(Path(backup_root))
    else:
        if server_name is None:
            raise ValueError("--server-name is required unless --backup-root is supplied")
        name = validate_server_name(server_name)
        desktop = resolved(Path(desktop_dir) if desktop_dir is not None else Path.home() / "Desktop")
        if not desktop.exists() or not desktop.is_dir() or desktop.is_symlink():
            raise ValueError("Desktop directory must be an existing real directory")
        root = desktop / f"{name}{BACKUP_SUFFIX}"
        if root.parent != desktop:
            raise ValueError("derived backup root must stay inside the selected Desktop directory")

    return BackupLayout(
        backup_root=root,
        discord_root=root / DISCORD_DIRNAME,
        core_root=root / CORE_DIRNAME,
    )


def validate_layout(layout: BackupLayout, workspace: Path) -> None:
    workspace = resolved(workspace)
    if paths_overlap(layout.backup_root, workspace):
        raise ValueError("backup root must not overlap the OpenClaw workspace")

    for path in (layout.backup_root, layout.discord_root, layout.core_root):
        if path.is_symlink():
            raise ValueError(f"backup destination must not be a symlink: {path}")
        if path.exists() and not path.is_dir():
            raise ValueError(f"backup destination must be a directory: {path}")

    parent = layout.backup_root.parent
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        raise ValueError("backup root parent must be an existing real directory")
    if not os.access(parent, os.W_OK):
        raise ValueError("backup root parent is not writable")
