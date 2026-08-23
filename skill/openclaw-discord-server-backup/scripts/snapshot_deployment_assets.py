#!/usr/bin/env python3
"""Create and verify a recovery bundle for customer deployment customizations."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RECOMMENDED_CUSTOM_ASSETS = {
    "installed_skill",
    "inventory_adapter",
    "adapter_wrapper",
    "classification_config",
    "mapping_ledger",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_assets(values: list[str]) -> dict[str, Path]:
    assets: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"asset must use label=path: {value}")
        label, raw_path = value.split("=", 1)
        if not label or "/" in label or "\\" in label or label in {".", ".."}:
            raise ValueError(f"unsafe asset label: {label}")
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        assets[label] = path
    return assets


def iter_regular_files(root: Path):
    if root.is_symlink():
        raise RuntimeError(f"symlink assets are not allowed: {root}")
    if root.is_file():
        yield root, Path(root.name)
        return
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"symlink assets are not allowed: {path}")
        if path.is_file():
            yield path, path.relative_to(root)


def create_bundle(out: Path, assets: dict[str, Path], required: set[str]) -> dict[str, Any]:
    missing = sorted(required - set(assets))
    if missing:
        raise RuntimeError(f"required recovery assets missing: {missing}")
    if out.exists():
        raise RuntimeError(f"recovery bundle already exists: {out}")
    entries: list[dict[str, Any]] = []
    out.mkdir(parents=True)
    for label, source in sorted(assets.items()):
        destination = out / "assets" / label
        if source.is_dir():
            shutil.copytree(source, destination)
            copied_root = destination
        else:
            destination.mkdir(parents=True)
            shutil.copy2(source, destination / source.name)
            copied_root = destination
        for path, relative in iter_regular_files(copied_root):
            entries.append({
                "path": str(Path("assets") / label / relative),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            })
    manifest = {
        "schema": "openclaw-deployment-recovery-v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "assets": sorted(assets),
        "requiredAssets": sorted(required),
        "files": len(entries),
        "bytes": sum(entry["bytes"] for entry in entries),
        "entries": entries,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify_bundle(bundle: Path) -> dict[str, Any]:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    expected = {entry["path"]: entry for entry in manifest.get("entries", [])}
    actual = {
        str(path.relative_to(bundle)): path
        for path in bundle.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    errors: list[str] = []
    if set(actual) != set(expected):
        errors.append("file_set_mismatch")
    for relative in sorted(set(actual) & set(expected)):
        path = actual[relative]
        entry = expected[relative]
        if path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
            errors.append(f"checksum_mismatch:{relative}")
    return {"ok": not errors, "files": len(actual), "errors": errors}


def restore_canary(bundle: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="openclaw-deployment-restore-") as tmp:
        restored = Path(tmp) / "bundle"
        shutil.copytree(bundle, restored)
        return verify_bundle(restored)


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot customer backup adapters and mapping evidence.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--out", required=True)
    create.add_argument("--asset", action="append", default=[])
    create.add_argument("--required", action="append", default=[])
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", required=True)
    canary = sub.add_parser("restore-canary")
    canary.add_argument("--bundle", required=True)
    args = parser.parse_args()
    if args.command == "create":
        result = create_bundle(Path(args.out), parse_assets(args.asset), set(args.required))
    elif args.command == "verify":
        result = verify_bundle(Path(args.bundle))
    else:
        result = restore_canary(Path(args.bundle))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
