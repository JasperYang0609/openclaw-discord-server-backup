from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill/openclaw-discord-server-backup"
SCRIPT = SKILL / "scripts/install.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("transactional_installer", SCRIPT)
installer = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = installer
spec.loader.exec_module(installer)


def fixture(tmp_path: Path, *, adoption: bool = False, prepared: bool = False):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    backup_root = tmp_path / "customer"
    discord_root = backup_root / "Discord資料"
    memory.mkdir(parents=True)
    discord_root.mkdir(parents=True)
    config = {
        "guildId": "123456789012345678",
        "backupRoot": str(discord_root),
        "statePath": "memory/state.json",
        "queuePath": "memory/queue.json",
        "reportChannel": "discord:channel:987654321098765432",
        "agentId": "main",
        "timezone": "Asia/Taipei",
    }
    if adoption:
        adoption_path = memory / "adoption.json"
        adoption_path.write_text("{}\n", encoding="utf-8")
        config["cron"] = {"adoptionMap": "memory/adoption.json"}
        if prepared:
            prepared_path = memory / "health/transactions/prepared"
            prepared_path.mkdir(parents=True)
            config["cron"]["preparedAdoptionReceipt"] = "memory/health/transactions/prepared"
    (memory / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (memory / "state.json").write_text(json.dumps({
        "version": 3, "schema": "channel-backup-state-v3",
        "guildId": config["guildId"], "rootPath": str(discord_root),
        "queuePath": "memory/queue.json", "entries": {},
    }), encoding="utf-8")
    (memory / "queue.json").write_text('{"version":1,"items":[]}\n', encoding="utf-8")
    args = argparse.Namespace(
        workspace=str(workspace), skill_dir=str(SKILL), config="memory/config.json",
        server_name=None, backup_root=None, desktop_dir=None, guild_id=None,
        report_to=None, agent=None, timezone=None, account_id=None,
        openclaw_bin=sys.executable, adoption_map=None, qwen_receipt=None,
        offline_scaffold=False, force=False, skip_canary=True,
        fault_after_cron=False,
    )
    return workspace, memory / "config.json", args


def stub_common(monkeypatch, args, events):
    monkeypatch.setattr(installer, "parse_args", lambda: args)
    monkeypatch.setattr(installer, "trusted_executable", lambda *_a, **_k: sys.executable)
    monkeypatch.setattr(
        installer, "stage_and_swap_skill",
        lambda source, target: events.append("skill-swap") or installer.SkillSwap(target, None, False),
    )
    monkeypatch.setattr(installer, "finalize_skill", lambda _swap: None)


def test_skill_tree_hash_detects_mode_only_drift(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_file = source / "helper.py"
    target_file = target / "helper.py"
    source_file.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    target_file.write_bytes(source_file.read_bytes())
    source_file.chmod(0o755)
    target_file.chmod(0o644)

    assert installer.skill_tree_hash(source) != installer.skill_tree_hash(target)

    target_file.chmod(0o755)
    assert installer.skill_tree_hash(source) == installer.skill_tree_hash(target)


def test_tree_hash_accepts_owned_internal_directory_alias(tmp_path):
    raw = tmp_path / "raw"
    canonical = raw / "customers/client"
    canonical.mkdir(parents=True)
    (canonical / "archive.md").write_text("one\n", encoding="utf-8")
    alias = raw / "legacy-client"
    alias.symlink_to(canonical, target_is_directory=True)

    before = installer.tree_hash(raw)
    (canonical / "archive.md").write_text("two\n", encoding="utf-8")

    assert installer.tree_hash(raw) != before


@pytest.mark.parametrize("target_kind", ["external", "broken", "chained", "file"])
def test_tree_hash_rejects_unsafe_directory_aliases(tmp_path, target_kind):
    raw = tmp_path / "raw"
    raw.mkdir()
    alias = raw / "alias"
    if target_kind == "external":
        target = tmp_path / "outside"
        target.mkdir()
    elif target_kind == "broken":
        target = raw / "missing"
    elif target_kind == "chained":
        target = raw / "target-link"
        real = raw / "real"
        real.mkdir()
        target.symlink_to(real, target_is_directory=True)
    else:
        target = raw / "file.txt"
        target.write_text("data\n", encoding="utf-8")
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(installer.InstallError):
        installer.tree_hash(raw)


def test_tree_hash_rejects_internal_alias_outside_current_owner(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    canonical = raw / "canonical"
    canonical.mkdir(parents=True)
    alias = raw / "legacy"
    alias.symlink_to(canonical, target_is_directory=True)
    actual_uid = os.getuid()
    monkeypatch.setattr(installer.os, "getuid", lambda: actual_uid + 1)

    with pytest.raises(installer.InstallError, match="unsafe symlink"):
        installer.tree_hash(raw)


def test_tree_hash_detects_internal_alias_retargeting(tmp_path):
    raw = tmp_path / "raw"
    canonical_a = raw / "canonical-a"
    canonical_b = raw / "canonical-b"
    canonical_a.mkdir(parents=True)
    canonical_b.mkdir(parents=True)
    alias = raw / "legacy"
    alias.symlink_to(canonical_a, target_is_directory=True)
    before = installer.tree_hash(raw)

    alias.unlink()
    alias.symlink_to(canonical_b, target_is_directory=True)

    assert installer.tree_hash(raw) != before


def test_tree_hash_rejects_alias_to_raw_root(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "alias").symlink_to(raw, target_is_directory=True)

    with pytest.raises(installer.InstallError, match="external symlink"):
        installer.tree_hash(raw)


def test_tree_hash_rejects_target_parent_swap_to_external_symlink(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    target_parent = raw / "customers"
    target = target_parent / "client"
    target.mkdir(parents=True)
    (target / "archive.md").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    (outside / "client").mkdir(parents=True)
    (outside / "client/archive.md").write_text("outside\n", encoding="utf-8")
    (raw / "legacy-client").symlink_to(target, target_is_directory=True)
    parked = raw / "customers-parked"
    original_open = installer.os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "customers" and kwargs.get("dir_fd") is not None and not swapped:
            target_parent.rename(parked)
            target_parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(installer.os, "open", racing_open)
    try:
        with pytest.raises(installer.InstallError, match="stable real directory"):
            installer.tree_hash(raw)
    finally:
        if target_parent.is_symlink():
            target_parent.unlink()
        if parked.exists():
            parked.rename(target_parent)


def test_tree_hash_rejects_raw_root_replacement_after_descriptor_open(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "archive.md").write_text("must be hashed\n", encoding="utf-8")
    parked = tmp_path / "raw-parked"
    original_open = installer.os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        fd = original_open(path, flags, *args, **kwargs)
        if Path(path) == raw and kwargs.get("dir_fd") is None and not swapped:
            raw.rename(parked)
            raw.mkdir()
            swapped = True
        return fd

    monkeypatch.setattr(installer.os, "open", racing_open)
    try:
        with pytest.raises(installer.InstallError, match="changed during integrity read"):
            installer.tree_hash(raw)
    finally:
        if raw.exists():
            raw.rmdir()
        if parked.exists():
            parked.rename(raw)


def test_tree_hash_rejects_alias_target_replacement_before_canonical_walk(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    target = raw / "z-target"
    target.mkdir()
    (target / "old.md").write_text("old\n", encoding="utf-8")
    (raw / "a-alias").symlink_to(target, target_is_directory=True)
    parked = raw / "z-target-parked"
    original_open_beneath = installer._open_directory_beneath
    swapped = False

    def racing_open_beneath(root_fd, parts, *, label):
        nonlocal swapped
        fd = original_open_beneath(root_fd, parts, label=label)
        if label == "raw alias target" and parts == ("z-target",) and not swapped:
            target.rename(parked)
            target.mkdir()
            (target / "new.md").write_text("new\n", encoding="utf-8")
            swapped = True
        return fd

    monkeypatch.setattr(installer, "_open_directory_beneath", racing_open_beneath)
    try:
        with pytest.raises(installer.InstallError, match="alias target changed"):
            installer.tree_hash(raw)
    finally:
        if target.exists():
            for child in target.iterdir():
                child.unlink()
            target.rmdir()
        if parked.exists():
            parked.rename(target)


def test_stage_and_swap_converges_mode_only_drift(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "scripts").mkdir(parents=True)
    (target / "scripts").mkdir(parents=True)
    source_file = source / "scripts/helper.py"
    target_file = target / "scripts/helper.py"
    source_file.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    target_file.write_bytes(source_file.read_bytes())
    source_file.chmod(0o755)
    # Group/other execute bits do not make an owner-owned file directly
    # executable when the owner's execute bit is absent.
    target_file.chmod(0o655)
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )

    swap = installer.stage_and_swap_skill(source, target)

    assert swap.changed is True
    assert installer.normalized_file_mode(target_file) == 0o755
    assert installer.skill_tree_hash(source) == installer.skill_tree_hash(target)
    installer.finalize_skill(swap)


def test_ordinary_upgrade_quiesces_before_first_file_mutation(tmp_path, monkeypatch):
    _workspace, _config_path, args = fixture(tmp_path)
    events = []
    stub_common(monkeypatch, args, events)
    monkeypatch.setattr(
        installer, "prepare_cron_quiescence",
        lambda *a, **k: events.append("quiesce") or {"status": "no_quiescence_needed", "transaction": None},
    )
    real_atomic = installer.atomic_if_changed
    monkeypatch.setattr(
        installer, "atomic_if_changed",
        lambda *a, **k: events.append("file-write") or real_atomic(*a, **k),
    )
    monkeypatch.setattr(
        installer, "run_cron_manager",
        lambda *a, **k: events.append("cron-apply") or {"status": "ready", "transaction": None},
    )
    assert installer.main() == 0
    assert events.index("quiesce") < events.index("file-write") < events.index("skill-swap") < events.index("cron-apply")


def test_adoption_upgrade_persists_prepared_receipt_and_passes_it_to_apply(tmp_path, monkeypatch):
    workspace, config_path, args = fixture(tmp_path, adoption=True)
    events = []
    stub_common(monkeypatch, args, events)
    transaction = workspace / "memory/health/transactions/new-prepared"
    transaction.mkdir(parents=True)
    monkeypatch.setattr(
        installer, "prepare_cron_quiescence",
        lambda *a, **k: {"status": "quiescence_prepared", "transaction": str(transaction)},
    )

    def fake_apply(*_args, **kwargs):
        assert kwargs["prepared_adoption_receipt"] == transaction
        return {"status": "ready", "transaction": None}

    monkeypatch.setattr(installer, "run_cron_manager", fake_apply)
    assert installer.main() == 0
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["cron"]["preparedAdoptionReceipt"] == "memory/health/transactions/new-prepared"


def test_identical_prepared_adoption_rerun_skips_pre_file_quiescence(tmp_path, monkeypatch):
    workspace, config_path, args = fixture(tmp_path, adoption=True, prepared=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["receiptDir"] = "memory/openclaw_discord_backup_health"
    config["workspaceSnapshot"] = {"includes": ["memory"]}
    config_path.write_bytes(installer.canonical_json(config))
    for name in ("state.json", "queue.json"):
        path = workspace / "memory" / name
        path.write_bytes(installer.canonical_json(json.loads(path.read_text(encoding="utf-8"))))
    receipt_dir = config_path.parent / "openclaw_discord_backup_health/components"
    receipt_dir.mkdir(parents=True)
    for component in ("weekly-inventory", "weekly-raw", "workspace-snapshot"):
        (receipt_dir / f"{component}.json").write_text("{}", encoding="utf-8")
    events = []
    stub_common(monkeypatch, args, events)
    monkeypatch.setattr(
        installer, "prepare_cron_quiescence",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no-op rerun must not quiesce")),
    )

    def fake_apply(*_args, **kwargs):
        assert kwargs["prepared_adoption_receipt"].name == "prepared"
        return {"status": "ready", "transaction": None, "mutations": 0}

    monkeypatch.setattr(installer, "run_cron_manager", fake_apply)
    assert installer.main() == 0


def test_rollback_incomplete_preserves_compatible_new_config(tmp_path, monkeypatch):
    _workspace, config_path, args = fixture(tmp_path)
    original = config_path.read_bytes()
    events = []
    stub_common(monkeypatch, args, events)
    monkeypatch.setattr(
        installer, "prepare_cron_quiescence",
        lambda *a, **k: {"status": "no_quiescence_needed", "transaction": None},
    )
    monkeypatch.setattr(
        installer, "run_cron_manager",
        lambda *a, **k: (_ for _ in ()).throw(installer.InstallRollbackIncomplete("uncertain cron rollback")),
    )
    assert installer.main() == 2
    assert config_path.read_bytes() != original
    assert "receiptDir" in json.loads(config_path.read_text(encoding="utf-8"))
    lock = config_path.parent / ".channel_backup.lock"
    descriptor = os.open(lock, os.O_RDWR)
    try:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_fresh_failure_restores_snapshotted_files_without_install_lock(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    args = argparse.Namespace(
        workspace=str(workspace), skill_dir=str(SKILL), config="memory/config.json",
        server_name="customer", backup_root=str(tmp_path / "customer"), desktop_dir=None,
        guild_id="123456789012345678", report_to="channel:987654321098765432",
        agent="main", timezone="Asia/Taipei", account_id=None,
        openclaw_bin=sys.executable, adoption_map=None, qwen_receipt=None,
        offline_scaffold=False, force=False, skip_canary=True, fault_after_cron=False,
    )
    events = []
    stub_common(monkeypatch, args, events)
    monkeypatch.setattr(
        installer, "run_cron_manager",
        lambda *a, **k: (_ for _ in ()).throw(installer.InstallError("injected")),
    )
    assert installer.main() == 2
    assert not (workspace / "memory/config.json").exists()
    assert not (workspace / "memory/channel_backup_summary_state.json").exists()
    assert not (workspace / "memory/channel_backup_backlog_queue.json").exists()


def test_snapshot_include_overlap_is_rejected_before_cron_mutation(tmp_path, monkeypatch):
    workspace, config_path, args = fixture(tmp_path)
    nested = workspace / "memory/nested"
    nested.mkdir()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["workspaceSnapshot"] = {"includes": ["memory", "memory/nested"]}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    events = []
    stub_common(monkeypatch, args, events)
    monkeypatch.setattr(
        installer,
        "prepare_cron_quiescence",
        lambda *a, **k: events.append("quiesce") or {"status": "no_quiescence_needed", "transaction": None},
    )
    monkeypatch.setattr(
        installer,
        "run_cron_manager",
        lambda *a, **k: events.append("cron-apply") or {"status": "ready", "transaction": None},
    )

    assert installer.main() == 2
    assert "cron-apply" not in events


def test_skill_rollback_failure_is_explicitly_incomplete(tmp_path, monkeypatch, capsys):
    _workspace, _config_path, args = fixture(tmp_path)
    args.fault_after_cron = True
    events = []
    stub_common(monkeypatch, args, events)
    monkeypatch.setattr(
        installer,
        "prepare_cron_quiescence",
        lambda *a, **k: {"status": "no_quiescence_needed", "transaction": None},
    )
    monkeypatch.setattr(
        installer,
        "run_cron_manager",
        lambda *a, **k: {"status": "ready", "transaction": None},
    )
    monkeypatch.setattr(
        installer,
        "rollback_skill",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("injected skill rollback failure")),
    )

    assert installer.main() == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "BLOCKED"
    assert payload["rollback"] == "incomplete"
    assert payload["runtimePreserved"] is False
    assert payload["runtimeState"] == "uncertain"
    assert any(item.startswith("restore-skill:PermissionError") for item in payload["rollbackErrors"])


def test_file_rollback_failure_is_explicitly_incomplete(tmp_path, monkeypatch, capsys):
    _workspace, _config_path, args = fixture(tmp_path)
    args.fault_after_cron = True
    events = []
    stub_common(monkeypatch, args, events)
    monkeypatch.setattr(
        installer,
        "prepare_cron_quiescence",
        lambda *a, **k: {"status": "no_quiescence_needed", "transaction": None},
    )
    monkeypatch.setattr(
        installer,
        "run_cron_manager",
        lambda *a, **k: {"status": "ready", "transaction": None},
    )
    monkeypatch.setattr(installer, "rollback_skill", lambda *_a, **_k: None)
    monkeypatch.setattr(
        installer,
        "restore_file",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("injected file rollback failure")),
    )

    assert installer.main() == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "BLOCKED"
    assert payload["rollback"] == "incomplete"
    assert payload["runtimePreserved"] is False
    assert payload["runtimeState"] == "uncertain"
    assert any(item.startswith("restore-file:") for item in payload["rollbackErrors"])
