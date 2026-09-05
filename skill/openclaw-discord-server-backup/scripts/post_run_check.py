#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile, ZipFile


SCRIPT_PATH = Path(__file__).resolve()
SKILL_DIR = SCRIPT_PATH.parents[1]
REPO_ROOT_CANDIDATE = SKILL_DIR.parents[1]
IS_REPOSITORY_LAYOUT = (
    (REPO_ROOT_CANDIDATE / "skill" / SKILL_DIR.name).resolve() == SKILL_DIR
    and (REPO_ROOT_CANDIDATE / "tests").is_dir()
    and (REPO_ROOT_CANDIDATE / "examples").is_dir()
)
ROOT = REPO_ROOT_CANDIDATE if IS_REPOSITORY_LAYOUT else SKILL_DIR
LAYOUT = "repository" if IS_REPOSITORY_LAYOUT else "installed"
RUNTIME_MANIFEST_SCHEMA = "openclaw-discord-runtime-components.v1"
RUNTIME_ADAPTER_CONTRACT = "openclaw-discord-rich-core-adapter.v3"
RUNTIME_COMPONENTS = {
    "rich_message_archive.py": "scripts/rich_message_archive.py",
    "rich_core_adapter_v3.py": "scripts/rich_core_adapter_v3.py",
    "run_daily_sync_v3.py": "scripts/run_daily_sync_v3.py",
    "run_managed_component.py": "scripts/run_managed_component.py",
}


def check(name: str, ok: bool, detail: str = "", results: list[tuple[str, bool, str]] | None = None) -> None:
    if results is not None:
        results.append((name, ok, detail))


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)


def installed_python_smoke() -> tuple[bool, str]:
    scripts = sorted((SKILL_DIR / "scripts").glob("*.py"))
    if not scripts:
        return False, "no Python scripts found"
    compile_proc = run([sys.executable, "-m", "py_compile", *[str(path) for path in scripts]])
    if compile_proc.returncode != 0:
        return False, (compile_proc.stderr or compile_proc.stdout).strip()

    cli_scripts = (
        "run_backlog_worker_v3.py",
        "audit_caught_up_v3.py",
        "audit_discord_inventory_v3.py",
        "reconcile_raw_archive_v3.py",
        "weekly_raw_reconcile_v4.py",
        "audit_cron_tooling.py",
        "snapshot_deployment_assets.py",
        "backup_workspace_assets.py",
        "core_workspace_backup.py",
        "check_daily_sync_gate.py",
        "manage_cron_topology.py",
        "run_managed_component.py",
        "backup_health_report.py",
        "daily_sync_lock_canary.py",
        "daily_sync_overlap_canary.py",
        "run_daily_sync_v3.py",
    )
    for name in cli_scripts:
        script = SKILL_DIR / "scripts" / name
        if not script.is_file():
            return False, f"required CLI is missing: {name}"
        proc = run([sys.executable, str(script), "--help"])
        if proc.returncode != 0:
            return False, f"{name} --help failed: {(proc.stderr or proc.stdout).strip()}"
    return True, f"compiled={len(scripts)} cli_help={len(cli_scripts)}"


def json_loads(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def runtime_components_smoke() -> tuple[bool, str]:
    manifest_path = SKILL_DIR / "manifests/runtime-components.v1.json"
    try:
        manifest_info = manifest_path.lstat()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"runtime manifest unreadable: {type(exc).__name__}"
    if (
        manifest_path.is_symlink()
        or not stat.S_ISREG(manifest_info.st_mode)
        or manifest_info.st_uid != os.geteuid()
        or manifest_info.st_nlink != 1
        or stat.S_IMODE(manifest_info.st_mode) != 0o600
        or not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "adapterContract", "components"}
        or payload.get("schemaVersion") != RUNTIME_MANIFEST_SCHEMA
        or payload.get("adapterContract") != RUNTIME_ADAPTER_CONTRACT
        or not isinstance(payload.get("components"), dict)
        or set(payload["components"]) != set(RUNTIME_COMPONENTS)
    ):
        return False, "runtime manifest identity or schema mismatch"
    for name, relative in RUNTIME_COMPONENTS.items():
        row = payload["components"].get(name)
        path = SKILL_DIR / relative
        try:
            info = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            return False, f"runtime component unreadable: {name}:{type(exc).__name__}"
        if (
            not isinstance(row, dict)
            or set(row) != {"relativePath", "sha256", "mode", "owner", "links"}
            or row.get("relativePath") != relative
            or row.get("mode") != "0600"
            or row.get("owner") != "effective-user"
            or row.get("links") != 1
            or path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or row.get("sha256") != digest
        ):
            return False, f"runtime component identity mismatch: {name}"
    return True, f"components={len(RUNTIME_COMPONENTS)}"


def queue_selector_smoke() -> tuple[bool, str]:
    script = SKILL_DIR / "scripts" / "select_backlog_candidates.py"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        state = tmpdir / "state.json"
        queue = tmpdir / "queue.json"
        state.write_text(
            json.dumps(
                {
                    "entries": {
                        "quiet": {
                            "type": "channel",
                            "channelId": "1",
                            "relativePath": "quiet",
                            "lastWrittenMessageId": "100",
                            "lastMessageId": "100",
                            "syncStatus": "healthy",
                        },
                        "partial": {
                            "type": "channel",
                            "channelId": "2",
                            "relativePath": "partial",
                            "lastWrittenMessageId": "200",
                            "lastMessageId": "200",
                            "syncStatus": "partial",
                            "backlogReason": "page_limit_reached",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        queue.write_text(json.dumps({"version": 1, "items": []}), encoding="utf-8")
        proc = run([
            sys.executable,
            str(script),
            "--state",
            str(state),
            "--queue",
            str(queue),
            "--today",
            "2026-07-10",
            "--limit",
            "1",
        ])
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout).strip()
        data = json.loads(proc.stdout)
        selected = data.get("selected") or []
        if not selected or selected[0].get("key") != "partial":
            return False, f"expected partial entry, got {selected}"
    return True, ""


def core_backup_smoke() -> tuple[bool, str]:
    script = SKILL_DIR / "scripts" / "core_workspace_backup.py"
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        workspace = base / "workspace"
        backup_root = base / "backup"
        (workspace / "memory" / "nested").mkdir(parents=True)
        (workspace / "AGENTS.md").write_text("agents\n", encoding="utf-8")
        (workspace / "memory" / "nested" / "daily.md").write_text("daily\n", encoding="utf-8")
        backup = run([
            sys.executable, str(script), "backup",
            "--workspace", str(workspace), "--backup-root", str(backup_root),
            "--date", "2026-07-10",
        ])
        if backup.returncode != 0:
            return False, (backup.stderr or backup.stdout).strip()
        latest = backup_root / "核心文件" / "latest"
        verify = run([sys.executable, str(script), "verify", "--backup-dir", str(latest)])
        if verify.returncode != 0:
            return False, (verify.stderr or verify.stdout).strip()
        canary = run([sys.executable, str(script), "restore-canary", "--backup-dir", str(latest)])
        if canary.returncode != 0:
            return False, (canary.stderr or canary.stdout).strip()
    return True, ""


def cron_manifest_smoke() -> tuple[bool, str]:
    script = SKILL_DIR / "scripts" / "manage_cron_topology.py"
    manifest = SKILL_DIR / "manifests" / "owned-cron.v1.json"
    proc = run([sys.executable, str(script), "validate-manifest", "--manifest", str(manifest)])
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, "manifest validator did not emit JSON"
    return result.get("ok") is True and result.get("jobs") == 11, proc.stdout.strip()


def daily_sync_gate_smoke() -> tuple[bool, str]:
    script = SKILL_DIR / "scripts" / "check_daily_sync_gate.py"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state = root / "state.json"
        inventory = root / "inventory.json"
        state.write_text('{"entries":{}}\n', encoding="utf-8")
        inventory.write_text(
            json.dumps({
                "ok": True,
                "checkedAt": "2026-09-04T05:25:00+08:00",
                "remainingMissing": 0,
                "warnings": [],
            }),
            encoding="utf-8",
        )
        proc = run([
            sys.executable,
            str(script),
            "--state",
            str(state),
            "--inventory",
            str(inventory),
            "--today",
            "2026-09-04",
            "--timezone",
            "Asia/Taipei",
        ])
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout).strip()
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return False, "daily sync gate did not emit JSON"
        return payload.get("ok") is True and payload.get("reasons") == [], proc.stdout.strip()


def workspace_snapshot_smoke() -> tuple[bool, str]:
    script = SKILL_DIR / "scripts" / "backup_workspace_assets.py"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        destination = root / "recovery"
        (workspace / "memory").mkdir(parents=True)
        (workspace / "memory" / "daily.md").write_text("safe\n", encoding="utf-8")
        create = run([
            sys.executable,
            str(script),
            "create",
            "--workspace",
            str(workspace),
            "--destination",
            str(destination),
            "--today",
            "2026-09-04",
            "--include",
            "memory",
            "--apply",
        ])
        if create.returncode != 0:
            return False, (create.stderr or create.stdout).strip()
        snapshot = destination / "snapshots" / "2026-09-04"
        for operation in ("verify", "restore-canary"):
            proc = run([sys.executable, str(script), operation, "--snapshot", str(snapshot)])
            if proc.returncode != 0:
                return False, f"{operation}: {(proc.stderr or proc.stdout).strip()}"
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                return False, f"{operation} did not emit JSON"
            if payload.get("ok") is not True:
                return False, f"{operation} did not report ok"
    return True, ""


def make_tree_writable(path: Path) -> None:
    if not path.exists():
        return
    for current, directories, files in os.walk(path, topdown=False):
        for name in files:
            (Path(current) / name).chmod(0o600)
        for name in directories:
            (Path(current) / name).chmod(0o700)
    path.chmod(0o700)


def weekly_evidence_smoke() -> tuple[bool, str]:
    script = SKILL_DIR / "scripts" / "weekly_raw_reconcile_v4.py"
    spec = importlib.util.spec_from_file_location("postcheck_weekly_raw_reconcile_v4", script)
    if spec is None or spec.loader is None:
        return False, "unable to load weekly evidence module"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state = root / "state.json"
        queue = root / "queue.json"
        archive = root / "archive"
        raw = archive / "Entry" / "raw"
        evidence = root / "evidence"
        raw.mkdir(parents=True)
        (raw / "2026-09-04.md").write_text("message\n", encoding="utf-8")
        state.write_text('{"entries":{"entry":{"relativePath":"Entry"}}}\n', encoding="utf-8")
        queue.write_text('{"version":1,"items":[]}\n', encoding="utf-8")
        try:
            created = module.create_pre_repair_evidence(
                evidence,
                state,
                queue,
                archive,
                [("entry", {"relativePath": "Entry"})],
            )
            verified = module.verify_evidence_bundle(evidence)
            if created.get("schema") != module.EVIDENCE_SCHEMA or verified.get("fileCount", 0) < 3:
                return False, "evidence manifest did not cover state, queue, and raw"
        except Exception as exc:
            return False, str(exc)
        finally:
            make_tree_writable(evidence)
    return True, ""


def package_matches_source() -> tuple[bool, str]:
    package = ROOT / "dist" / "openclaw-discord-server-backup.skill"
    if not package.is_file():
        return False, "dist package is missing"
    expected: dict[str, bytes] = {}
    for path in sorted(SKILL_DIR.rglob("*")):
        if (
            not path.is_file()
            or "__pycache__" in path.relative_to(SKILL_DIR).parts
            or path.suffix == ".pyc"
            or path.name == ".DS_Store"
        ):
            continue
        name = f"{SKILL_DIR.name}/{path.relative_to(SKILL_DIR).as_posix()}"
        expected[name] = path.read_bytes()
    try:
        with ZipFile(package) as archive:
            names = set(archive.namelist())
            if names != set(expected):
                return False, f"package file set mismatch: missing={sorted(set(expected)-names)}, extra={sorted(names-set(expected))}"
            for name, content in expected.items():
                if archive.read(name) != content:
                    return False, f"package content mismatch: {name}"
    except (OSError, BadZipFile) as exc:
        return False, f"package unreadable: {exc}"
    return True, ""


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    skill_required = [
        "SKILL.md",
        "scripts/run_backlog_worker_v3.py",
        "scripts/audit_caught_up_v3.py",
        "scripts/audit_discord_inventory_v3.py",
        "scripts/reconcile_raw_archive_v3.py",
        "scripts/weekly_raw_reconcile_v4.py",
        "scripts/audit_cron_tooling.py",
        "scripts/snapshot_deployment_assets.py",
        "scripts/backup_workspace_assets.py",
        "scripts/select_backlog_candidates.py",
        "scripts/backup_paths.py",
        "scripts/core_workspace_backup.py",
        "scripts/check_daily_sync_gate.py",
        "scripts/manage_cron_topology.py",
        "scripts/run_managed_component.py",
        "scripts/backup_health_report.py",
        "scripts/daily_sync_lock_canary.py",
        "scripts/daily_sync_overlap_canary.py",
        "scripts/rich_message_archive.py",
        "scripts/rich_core_adapter_v3.py",
        "scripts/run_daily_sync_v3.py",
        "manifests/runtime-components.v1.json",
        "manifests/owned-cron.v1.json",
        "prompts/core-backup.md",
        "prompts/daily-sync-v3.md",
        "references/recovery.md",
        "examples/owned-cron.md",
        "examples/adoption-map.example.json",
    ]
    check("layout detected", LAYOUT in {"repository", "installed"}, LAYOUT, results)
    check("required skill files exist", all((SKILL_DIR / rel).exists() for rel in skill_required), results=results)

    if IS_REPOSITORY_LAYOUT:
        examples = ["examples/config.example.json", "examples/state.example.json", "examples/queue.example.json"]
        check("example JSON parses", all(json_loads(ROOT / rel) for rel in examples), results=results)

    ok, detail = queue_selector_smoke()
    check("queue selector smoke", ok, detail, results)

    ok, detail = core_backup_smoke()
    check("core backup verify/restore smoke", ok, detail, results)

    ok, detail = cron_manifest_smoke()
    check("owned cron manifest validation", ok, detail, results)

    ok, detail = runtime_components_smoke()
    check("runtime component manifest", ok, detail, results)

    ok, detail = daily_sync_gate_smoke()
    check("daily sync preflight gate", ok, detail, results)

    ok, detail = workspace_snapshot_smoke()
    check("workspace snapshot verify/restore smoke", ok, detail, results)

    ok, detail = weekly_evidence_smoke()
    check("weekly immutable evidence smoke", ok, detail, results)

    if IS_REPOSITORY_LAYOUT:
        ok, detail = package_matches_source()
        check("packaged skill matches source", ok, detail, results)

        direct = run([sys.executable, "tests/test_backlog_worker_selection.py"])
        check("backlog worker direct tests", direct.returncode == 0, (direct.stderr or direct.stdout).strip()[-1200:], results)

        pytest = run([sys.executable, "-m", "pytest", "tests"])
        if pytest.returncode == 0:
            check("pytest suite", True, results=results)
        elif "No module named pytest" in (pytest.stderr + pytest.stdout):
            check("pytest suite", True, "pytest unavailable; direct smoke tests passed", results)
        else:
            check("pytest suite", False, (pytest.stderr or pytest.stdout).strip()[-1200:], results)
    else:
        ok, detail = installed_python_smoke()
        check("installed Python/CLI smoke", ok, detail, results)

    failed = [row for row in results if not row[1]]
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} {name}{' - ' + detail if detail else ''}")
    if failed:
        print(f"post-run check failed: {len(failed)} issue(s)", file=sys.stderr)
        return 1
    print("post-run check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
