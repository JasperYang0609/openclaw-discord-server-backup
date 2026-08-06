from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "openclaw-discord-server-backup" / "scripts" / "core_workspace_backup.py"
DATE = "2026-08-06"


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode())
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class CoreWorkspaceBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.workspace = base / "workspace"
        self.backup_root = base / "backup"
        self.workspace.mkdir()
        (self.workspace / "AGENTS.md").write_text("agents\n", encoding="utf-8")
        (self.workspace / "USER.md").write_text("user\n", encoding="utf-8")
        (self.workspace / "ignore.txt").write_text("ignore\n", encoding="utf-8")
        (self.workspace / "other").mkdir()
        (self.workspace / "other" / "not-in-scope.md").write_text("no\n", encoding="utf-8")
        (self.workspace / "memory" / "nested" / "empty").mkdir(parents=True)
        (self.workspace / "memory" / "today.md").write_text("daily\n", encoding="utf-8")
        (self.workspace / "memory" / "nested" / "project.md").write_text("project\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, command: str, *extra: str):
        return subprocess.run(
            [sys.executable, str(SCRIPT), command, *extra],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def backup(self, *, date: str = DATE, workspace: Path | None = None, backup_root: Path | None = None):
        return self.run_cli(
            "backup",
            "--workspace",
            str(workspace or self.workspace),
            "--backup-root",
            str(backup_root or self.backup_root),
            "--date",
            date,
        )

    @property
    def latest(self) -> Path:
        return self.backup_root / "核心文件" / "latest"

    @property
    def snapshot(self) -> Path:
        return self.backup_root / "核心文件" / "snapshots" / DATE

    def assert_verify_passes(self, path: Path):
        proc = self.run_cli("verify", "--backup-dir", str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_backup_covers_exact_root_markdown_and_memory_tree(self):
        proc = self.backup()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["snapshotStatus"], "created")
        self.assertEqual(result["rootMarkdownCount"], 2)
        self.assertEqual(result["memoryFiles"], 2)

        for tree in (self.latest, self.snapshot):
            verified = self.assert_verify_passes(tree)
            self.assertEqual(verified["files"], 4)
            self.assertTrue((tree / "AGENTS.md").is_file())
            self.assertTrue((tree / "USER.md").is_file())
            self.assertTrue((tree / "memory/nested/empty").is_dir())
            self.assertFalse((tree / "ignore.txt").exists())
            self.assertFalse((tree / "other").exists())
            self.assertTrue((tree / ".backup-manifest.json").is_file())

    def test_latest_replacement_removes_stale_files_and_snapshot_is_immutable(self):
        first = self.backup()
        self.assertEqual(first.returncode, 0, first.stderr)
        snapshot_before = tree_digest(self.snapshot)
        (self.workspace / "USER.md").unlink()
        (self.workspace / "NEW.md").write_text("new\n", encoding="utf-8")
        (self.workspace / "memory/today.md").write_text("updated\n", encoding="utf-8")

        second = self.backup()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["snapshotStatus"], "existing")
        self.assertFalse((self.latest / "USER.md").exists())
        self.assertEqual((self.latest / "NEW.md").read_text(encoding="utf-8"), "new\n")
        self.assertEqual((self.latest / "memory/today.md").read_text(encoding="utf-8"), "updated\n")
        self.assertEqual(tree_digest(self.snapshot), snapshot_before)
        self.assertEqual((self.snapshot / "USER.md").read_text(encoding="utf-8"), "user\n")
        self.assert_verify_passes(self.latest)
        self.assert_verify_passes(self.snapshot)

    def test_tampered_file_is_detected(self):
        self.assertEqual(self.backup().returncode, 0)
        (self.latest / "AGENTS.md").write_text("tampered\n", encoding="utf-8")
        proc = self.run_cli("verify", "--backup-dir", str(self.latest))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mismatch", proc.stderr)

    def test_missing_file_is_detected(self):
        self.assertEqual(self.backup().returncode, 0)
        (self.latest / "memory/today.md").unlink()
        proc = self.run_cli("verify", "--backup-dir", str(self.latest))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("missing_files", proc.stderr)

    def test_extra_file_is_detected(self):
        self.assertEqual(self.backup().returncode, 0)
        (self.latest / "extra.md").write_text("extra\n", encoding="utf-8")
        proc = self.run_cli("verify", "--backup-dir", str(self.latest))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("extra_files", proc.stderr)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_source_symlink_is_rejected_without_committing_backup(self):
        (self.workspace / "memory" / "linked.md").symlink_to(self.workspace / "AGENTS.md")
        proc = self.backup()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("symlink", proc.stderr)
        self.assertFalse(self.latest.exists())
        self.assertFalse(self.snapshot.exists())


    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_destination_management_symlink_is_rejected(self):
        self.backup_root.mkdir()
        external = Path(self.temp.name) / "external"
        external.mkdir()
        (self.backup_root / "核心文件").symlink_to(external, target_is_directory=True)
        proc = self.backup()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must not be a symlink", proc.stderr)
        self.assertEqual(list(external.iterdir()), [])

    def test_concurrent_backup_lock_fails_closed(self):
        self.backup_root.mkdir()
        core_root = self.backup_root / "核心文件"
        core_root.mkdir()
        lock_path = core_root / ".core-backup.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc = self.backup()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already running", proc.stderr)
        self.assertFalse(self.latest.exists())

    def test_workspace_and_backup_overlap_is_rejected(self):
        nested_backup = self.workspace / "backup"
        proc = self.backup(backup_root=nested_backup)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must not overlap", proc.stderr)
        self.assertFalse(nested_backup.exists())

    def test_restore_canary_does_not_write_to_workspace(self):
        self.assertEqual(self.backup().returncode, 0)
        before = tree_digest(self.workspace)
        proc = self.run_cli("restore-canary", "--backup-dir", str(self.latest))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["restoreCanary"], "passed")
        self.assertEqual(tree_digest(self.workspace), before)
        self.assertFalse(any(path.name.startswith("openclaw-core-restore-canary-") for path in Path(tempfile.gettempdir()).iterdir()))

    def test_corrupt_existing_snapshot_fails_closed_before_latest_changes(self):
        self.assertEqual(self.backup().returncode, 0)
        latest_before = tree_digest(self.latest)
        (self.snapshot / "AGENTS.md").write_text("corrupt\n", encoding="utf-8")
        (self.workspace / "AGENTS.md").write_text("new workspace value\n", encoding="utf-8")
        proc = self.backup()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mismatch", proc.stderr)
        self.assertEqual(tree_digest(self.latest), latest_before)

    def test_invalid_snapshot_date_is_rejected(self):
        proc = self.backup(date="../../escape")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("YYYY-MM-DD", proc.stderr)
        self.assertFalse(self.backup_root.exists())


if __name__ == "__main__":
    unittest.main()
