from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "openclaw-discord-server-backup"
PACKAGER = SKILL / "scripts" / "package_skill.py"


class PackageSkillTests(unittest.TestCase):
    def build(self, output_dir: Path) -> Path:
        proc = subprocess.run(
            [sys.executable, str(PACKAGER), str(SKILL), str(output_dir)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return Path(proc.stdout.strip())

    def test_package_is_reproducible_and_matches_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first = self.build(base / "one")
            second = self.build(base / "two")
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), hashlib.sha256(second.read_bytes()).hexdigest())
            with ZipFile(first) as archive:
                names = set(archive.namelist())
                self.assertIn("openclaw-discord-server-backup/scripts/core_workspace_backup.py", names)
                self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))
                for source in sorted(SKILL.rglob("*")):
                    if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc" or source.name == ".DS_Store":
                        continue
                    archived = "openclaw-discord-server-backup/" + source.relative_to(SKILL).as_posix()
                    self.assertIn(archived, names)
                    self.assertEqual(archive.read(archived), source.read_bytes(), archived)


if __name__ == "__main__":
    unittest.main()
