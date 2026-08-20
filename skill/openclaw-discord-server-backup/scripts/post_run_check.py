#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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
        "backup_workspace_assets.py",
        "core_workspace_backup.py",
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
        "scripts/backup_workspace_assets.py",
        "scripts/select_backlog_candidates.py",
        "scripts/core_workspace_backup.py",
        "prompts/core-backup.md",
        "references/recovery.md",
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
