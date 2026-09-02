from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "openclaw-discord-server-backup"
INSTALLER = SKILL / "scripts" / "install.py"
CORE_BACKUP = SKILL / "scripts" / "core_workspace_backup.py"


class InstallerDesktopBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.desktop = self.base / "home" / "Desktop"
        self.workspace.mkdir()
        self.desktop.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, *extra: str, force: bool = False) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(INSTALLER),
            "--workspace",
            str(self.workspace),
            "--desktop-dir",
            str(self.desktop),
            *extra,
        ]
        if force:
            command.append("--force")
        return subprocess.run(command, cwd=ROOT, text=True, capture_output=True)

    def test_fresh_install_creates_named_real_root_and_matching_paths(self):
        proc = self.install("--server-name", "  南方  ")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        backup_root = self.desktop / "南方資料備份"
        discord_root = backup_root / "Discord資料"

        self.assertTrue(backup_root.is_dir())
        self.assertFalse(backup_root.is_symlink())
        self.assertTrue(discord_root.is_dir())
        self.assertEqual(Path(result["backupRoot"]), backup_root)
        self.assertEqual(Path(result["discordDataRoot"]), discord_root)
        self.assertEqual(Path(result["coreDataRoot"]), backup_root / "核心文件")

        config = json.loads((self.workspace / "memory/openclaw_discord_backup_config.json").read_text(encoding="utf-8"))
        state = json.loads((self.workspace / "memory/channel_backup_summary_state.json").read_text(encoding="utf-8"))
        self.assertEqual(config["backupRoot"], str(discord_root))
        self.assertEqual(state["rootPath"], str(discord_root))

    def test_core_engine_writes_under_same_customer_root(self):
        proc = self.install("--server-name", "南方")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        backup_root = Path(json.loads(proc.stdout)["backupRoot"])
        (self.workspace / "AGENTS.md").write_text("agents\n", encoding="utf-8")
        (self.workspace / "memory" / "today.md").write_text("daily\n", encoding="utf-8")

        backup = subprocess.run(
            [
                sys.executable,
                str(CORE_BACKUP),
                "backup",
                "--workspace",
                str(self.workspace),
                "--backup-root",
                str(backup_root),
                "--date",
                "2026-09-02",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(backup.returncode, 0, backup.stderr)
        self.assertTrue((backup_root / "核心文件/latest/AGENTS.md").is_file())
        self.assertTrue((backup_root / "核心文件/snapshots/2026-09-02/memory/today.md").is_file())

    def test_rerun_preserves_existing_backup_files(self):
        first = self.install("--server-name", "南方")
        self.assertEqual(first.returncode, 0, first.stderr)
        sentinel = self.desktop / "南方資料備份/Discord資料/keep.md"
        sentinel.write_text("keep\n", encoding="utf-8")

        second = self.install("--server-name", "南方", force=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_unsafe_server_names_fail_before_writes(self):
        for name in ("", ".", "..", "a/b", "a\\b", "bad\x01name"):
            with self.subTest(name=repr(name)):
                proc = self.install("--server-name", name)
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse((self.workspace / "skills").exists())
                self.assertEqual(list(self.desktop.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_destination_fails_closed(self):
        external = self.base / "external"
        external.mkdir()
        (self.desktop / "南方資料備份").symlink_to(external, target_is_directory=True)
        proc = self.install("--server-name", "南方")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse((self.workspace / "skills").exists())

    def test_file_destination_fails_closed(self):
        (self.desktop / "南方資料備份").write_text("occupied\n", encoding="utf-8")
        proc = self.install("--server-name", "南方")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.workspace / "skills").exists())

    def test_explicit_root_wins_without_creating_desktop_default(self):
        custom = self.base / "custom-backup"
        proc = self.install("--backup-root", str(custom))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(Path(result["backupRoot"]), custom)
        self.assertTrue((custom / "Discord資料").is_dir())
        self.assertEqual(list(self.desktop.iterdir()), [])

    def test_custom_root_overlapping_workspace_fails_closed(self):
        proc = self.install("--backup-root", str(self.workspace / "backup"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must not overlap", proc.stderr)
        self.assertFalse((self.workspace / "backup").exists())
        self.assertFalse((self.workspace / "skills").exists())

    def test_existing_different_root_requires_migration_without_writes(self):
        memory = self.workspace / "memory"
        memory.mkdir()
        config = {
            "backupRoot": str(self.base / "legacy" / "discord"),
            "statePath": "memory/channel_backup_summary_state.json",
            "queuePath": "memory/channel_backup_backlog_queue.json",
        }
        (memory / "openclaw_discord_backup_config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        proc = self.install("--server-name", "南方")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("migration is required", proc.stderr)
        self.assertFalse((self.desktop / "南方資料備份").exists())
        self.assertFalse((self.workspace / "skills").exists())


if __name__ == "__main__":
    unittest.main()
