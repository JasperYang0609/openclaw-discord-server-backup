#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


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


def iter_files(source: Path, patterns: Iterable[str]) -> Iterable[Path]:
    if source.is_file():
        if not source.is_symlink() and not is_excluded(Path(source.name), patterns):
            yield source
        return
    for root, dirnames, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        dirnames[:] = [
            name for name in dirnames
            if not (root_path / name).is_symlink()
            and not is_excluded(relative_root / name, patterns)
        ]
        for filename in filenames:
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


def copy_source(source: Path, destination: Path, patterns: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in iter_files(source, patterns):
        relative = Path(path.name) if source.is_file() else path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        rows.append({
            "path": relative.as_posix(),
            "bytes": target.stat().st_size,
            "sha256": sha256_file(target),
        })
    return rows


def build_snapshot(
    workspace: Path,
    destination_root: Path,
    today: str,
    includes: list[str],
    *,
    root_markdown: bool,
    excludes: list[str],
) -> dict[str, Any]:
    destination_root.mkdir(parents=True, exist_ok=True)
    final = destination_root / "snapshots" / today
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        raise FileExistsError(f"Snapshot already exists: {final}")
    temp = Path(tempfile.mkdtemp(prefix=f".{today}-", dir=final.parent))
    manifest: dict[str, Any] = {
        "version": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "workspace": str(workspace),
        "snapshot": str(final),
        "excludes": excludes,
        "sources": [],
    }
    try:
        if root_markdown:
            root_files = []
            for path in sorted(workspace.glob("*.md")):
                if path.is_symlink() or is_excluded(Path(path.name), excludes):
                    continue
                target = temp / "root_md" / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                root_files.append({"path": path.name, "bytes": target.stat().st_size, "sha256": sha256_file(target)})
            manifest["sources"].append({"source": "root_md", "files": root_files})
        for relative_name in includes:
            source = (workspace / relative_name).resolve()
            if not source.exists():
                manifest["sources"].append({"source": relative_name, "missing": True, "files": []})
                continue
            try:
                source.relative_to(workspace.resolve())
            except ValueError as exc:
                raise ValueError(f"Include escapes workspace: {relative_name}") from exc
            files = copy_source(source, temp / "workspace" / relative_name, excludes)
            manifest["sources"].append({"source": relative_name, "files": files})
        manifest["fileCount"] = sum(len(source.get("files") or []) for source in manifest["sources"])
        manifest["bytes"] = sum(
            row["bytes"]
            for source in manifest["sources"]
            for row in (source.get("files") or [])
        )
        (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, final)
        (destination_root / "latest.json").write_text(
            json.dumps({"snapshot": str(final), "createdAt": manifest["createdAt"], "fileCount": manifest["fileCount"], "bytes": manifest["bytes"]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a checksummed snapshot of selected workspace recovery assets.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--today", default=datetime.now().astimezone().date().isoformat())
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--root-markdown", action="store_true")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    destination = Path(args.destination).expanduser().resolve()
    excludes = list(DEFAULT_EXCLUDES) + args.exclude
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
        print(json.dumps({"mode": "skipped", "reason": "snapshot_exists", "snapshot": str(destination / "snapshots" / args.today)}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({
        "mode": "apply",
        "snapshot": manifest["snapshot"],
        "fileCount": manifest["fileCount"],
        "bytes": manifest["bytes"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
