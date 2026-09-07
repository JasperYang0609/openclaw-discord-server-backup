from __future__ import annotations

import hashlib
import importlib.util
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "openclaw-discord-server-backup"
PACKAGER = SKILL / "scripts" / "package_skill.py"
POST_CHECK = SKILL / "scripts" / "post_run_check.py"
DIRECT_EXECUTABLES = (
    "scripts/check_daily_sync_gate.py",
    "scripts/backup_health_report.py",
)


def load_post_check():
    spec = importlib.util.spec_from_file_location("package_test_post_run_check", POST_CHECK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_with_archive_modes(package: Path, destination: Path) -> None:
    with ZipFile(package) as archive:
        archive.extractall(destination)
        for info in archive.infolist():
            extracted = destination / info.filename
            if extracted.is_file():
                extracted.chmod((info.external_attr >> 16) & 0o777)


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

    def test_package_normalizes_explicit_fixture_modes_and_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = base / "mode-fixture"
            scripts = fixture / "scripts"
            scripts.mkdir(parents=True)
            (fixture / "SKILL.md").write_text("# Fixture\n", encoding="utf-8")
            executable = scripts / "direct-helper.py"
            regular = scripts / "library.py"
            owner_non_executable = scripts / "owner-non-executable.py"
            executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            regular.write_text("VALUE = 1\n", encoding="utf-8")
            owner_non_executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            executable.chmod(0o700)
            regular.chmod(0o600)
            owner_non_executable.chmod(0o655)

            proc = subprocess.run(
                [sys.executable, str(PACKAGER), str(fixture), str(base / "dist")],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            package = Path(proc.stdout.strip())
            with ZipFile(package) as archive:
                executable_info = archive.getinfo("mode-fixture/scripts/direct-helper.py")
                regular_info = archive.getinfo("mode-fixture/scripts/library.py")
                owner_non_executable_info = archive.getinfo(
                    "mode-fixture/scripts/owner-non-executable.py"
                )
                self.assertEqual(
                    stat.S_IFMT((executable_info.external_attr >> 16) & 0xFFFF),
                    stat.S_IFREG,
                )
                self.assertEqual(
                    stat.S_IMODE((executable_info.external_attr >> 16) & 0xFFFF),
                    0o755,
                )
                self.assertEqual(
                    stat.S_IMODE((regular_info.external_attr >> 16) & 0xFFFF),
                    0o644,
                )
                self.assertEqual(
                    (owner_non_executable_info.external_attr >> 16) & 0o777,
                    0o644,
                )

            extracted = base / "extracted"
            extract_with_archive_modes(package, extracted)
            executable_mode = (extracted / "mode-fixture/scripts/direct-helper.py").stat().st_mode
            regular_mode = (extracted / "mode-fixture/scripts/library.py").stat().st_mode
            owner_non_executable_mode = (
                extracted / "mode-fixture/scripts/owner-non-executable.py"
            ).stat().st_mode
            self.assertEqual(stat.S_IMODE(executable_mode), 0o755)
            self.assertEqual(stat.S_IMODE(regular_mode), 0o644)
            self.assertEqual(stat.S_IMODE(owner_non_executable_mode), 0o644)

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
                    expected_mode = 0o755 if source.stat().st_mode & stat.S_IXUSR else 0o644
                    info = archive.getinfo(archived)
                    unix_mode = (info.external_attr >> 16) & 0xFFFF
                    self.assertEqual(info.create_system, 3, archived)
                    self.assertEqual(stat.S_IFMT(unix_mode), stat.S_IFREG, archived)
                    archived_mode = stat.S_IMODE(unix_mode)
                    self.assertEqual(archived_mode, expected_mode, archived)

    def test_packaged_post_run_check_passes_in_installed_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = self.build(base / "dist")
            installed_root = base / "installed"
            extract_with_archive_modes(package, installed_root)
            skill = installed_root / "openclaw-discord-server-backup"
            for relative in DIRECT_EXECUTABLES:
                mode = (skill / relative).stat().st_mode
                self.assertTrue(stat.S_IMODE(mode) & 0o111, relative)
            proc = subprocess.run(
                [sys.executable, str(skill / "scripts/post_run_check.py")],
                cwd=skill,
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("PASS layout detected - installed", proc.stdout)
            self.assertIn("PASS installed Python/CLI smoke", proc.stdout)
            self.assertIn("post-run check passed", proc.stdout)

    def test_installed_post_run_check_rejects_stale_non_executable_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = self.build(base / "dist")
            installed_root = base / "installed"
            extract_with_archive_modes(package, installed_root)
            skill = installed_root / "openclaw-discord-server-backup"
            stale = skill / DIRECT_EXECUTABLES[0]
            for mode in (0o644, 0o655, 0o641):
                with self.subTest(mode=oct(mode)):
                    stale.chmod(mode)
                    proc = subprocess.run(
                        [sys.executable, str(skill / "scripts/post_run_check.py")],
                        cwd=skill,
                        text=True,
                        capture_output=True,
                    )

                    self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertIn("FAIL directly invoked helpers executable", proc.stdout)
                    self.assertIn(DIRECT_EXECUTABLES[0], proc.stdout)

    def test_package_match_rejects_executable_mode_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = self.build(base / "dist")
            tampered = base / "mode-mismatch.skill"
            target = "openclaw-discord-server-backup/" + DIRECT_EXECUTABLES[1]
            with ZipFile(package) as source, ZipFile(tampered, "w", ZIP_DEFLATED) as output:
                for info in source.infolist():
                    rewritten = ZipInfo(info.filename, date_time=info.date_time)
                    rewritten.compress_type = ZIP_DEFLATED
                    rewritten.create_system = 3
                    rewritten.external_attr = info.external_attr
                    if info.filename == target:
                        rewritten.external_attr = (stat.S_IFREG | 0o644) << 16
                    output.writestr(rewritten, source.read(info.filename))

            ok, detail = load_post_check().package_matches_source(tampered, SKILL)

            self.assertFalse(ok)
            self.assertIn("package mode mismatch", detail)
            self.assertIn(DIRECT_EXECUTABLES[1], detail)

    def test_package_match_rejects_non_unix_member_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = self.build(base / "dist")
            tampered = base / "non-unix.skill"
            target = "openclaw-discord-server-backup/" + DIRECT_EXECUTABLES[0]
            with ZipFile(package) as source, ZipFile(tampered, "w", ZIP_DEFLATED) as output:
                for info in source.infolist():
                    rewritten = ZipInfo(info.filename, date_time=info.date_time)
                    rewritten.compress_type = ZIP_DEFLATED
                    rewritten.create_system = 0 if info.filename == target else info.create_system
                    rewritten.external_attr = info.external_attr
                    output.writestr(rewritten, source.read(info.filename))

            ok, detail = load_post_check().package_matches_source(tampered, SKILL)

            self.assertFalse(ok)
            self.assertIn("not Unix metadata", detail)

    def test_package_match_rejects_symlink_file_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = self.build(base / "dist")
            tampered = base / "symlink-type.skill"
            target = "openclaw-discord-server-backup/" + DIRECT_EXECUTABLES[0]
            with ZipFile(package) as source, ZipFile(tampered, "w", ZIP_DEFLATED) as output:
                for info in source.infolist():
                    rewritten = ZipInfo(info.filename, date_time=info.date_time)
                    rewritten.compress_type = ZIP_DEFLATED
                    rewritten.create_system = info.create_system
                    rewritten.external_attr = info.external_attr
                    if info.filename == target:
                        rewritten.external_attr = (stat.S_IFLNK | 0o755) << 16
                    output.writestr(rewritten, source.read(info.filename))

            ok, detail = load_post_check().package_matches_source(tampered, SKILL)

            self.assertFalse(ok)
            self.assertIn("not a regular file", detail)

    def test_package_match_rejects_duplicate_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = self.build(base / "dist")
            tampered = base / "duplicate.skill"
            target = "openclaw-discord-server-backup/" + DIRECT_EXECUTABLES[0]
            with ZipFile(package) as source, ZipFile(tampered, "w", ZIP_DEFLATED) as output:
                for info in source.infolist():
                    output.writestr(info, source.read(info.filename))
                info = source.getinfo(target)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    output.writestr(info, source.read(target))

            ok, detail = load_post_check().package_matches_source(tampered, SKILL)

            self.assertFalse(ok)
            self.assertIn("duplicate members", detail)


if __name__ == "__main__":
    unittest.main()
