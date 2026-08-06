#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def include_file(path: Path, skill_dir: Path) -> bool:
    relative = path.relative_to(skill_dir)
    return (
        path.is_file()
        and "__pycache__" not in relative.parts
        and path.suffix != ".pyc"
        and path.name != ".DS_Store"
    )


def package(skill_dir: Path, output: Path) -> None:
    if not (skill_dir / "SKILL.md").is_file():
        raise SystemExit("SKILL.md not found")
    if any(path.is_symlink() for path in skill_dir.rglob("*")):
        raise SystemExit("Symlinks are not allowed in skill packages")

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with ZipFile(temp_path, "w", ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(skill_dir.rglob("*")):
                if not include_file(path, skill_dir):
                    continue
                relative = skill_dir.name / path.relative_to(skill_dir)
                info = ZipInfo(relative.as_posix(), date_time=FIXED_ZIP_TIME)
                info.compress_type = ZIP_DEFLATED
                mode = 0o755 if os.access(path, os.X_OK) else 0o644
                info.external_attr = (mode & 0xFFFF) << 16
                info.create_system = 3
                archive.writestr(info, path.read_bytes())
        temp_path.replace(output)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Package an OpenClaw skill folder as a deterministic .skill zip.")
    parser.add_argument("skill_dir")
    parser.add_argument("out_dir", nargs="?", default="dist")
    args = parser.parse_args()

    skill_dir = Path(args.skill_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    output = out_dir / f"{skill_dir.name}.skill"
    package(skill_dir, output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
