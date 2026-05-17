#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


def main() -> int:
    ap = argparse.ArgumentParser(description="Package an OpenClaw skill folder as .skill zip.")
    ap.add_argument("skill_dir")
    ap.add_argument("out_dir", nargs="?", default="dist")
    args = ap.parse_args()

    skill_dir = Path(args.skill_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    if not (skill_dir / "SKILL.md").exists():
        raise SystemExit("SKILL.md not found")
    if any(p.is_symlink() for p in skill_dir.rglob("*")):
        raise SystemExit("Symlinks are not allowed in skill packages")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{skill_dir.name}.skill"
    with ZipFile(out, "w", ZIP_DEFLATED) as z:
        for p in sorted(skill_dir.rglob("*")):
            if p.is_file():
                z.write(p, skill_dir.name / p.relative_to(skill_dir))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
