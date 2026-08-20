import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/backup_workspace_assets.py"
spec = importlib.util.spec_from_file_location("backup_workspace_assets", SCRIPT)
backup = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(backup)


def test_snapshot_copies_selected_assets_and_root_markdown(tmp_path: Path):
    workspace = tmp_path / "workspace"
    destination = tmp_path / "backup"
    (workspace / "records").mkdir(parents=True)
    (workspace / "records" / "evidence.md").write_text("evidence", encoding="utf-8")
    (workspace / "records" / ".env").write_text("secret", encoding="utf-8")
    (workspace / "README.md").write_text("readme", encoding="utf-8")

    result = backup.build_snapshot(
        workspace,
        destination,
        "2026-08-20",
        ["records"],
        root_markdown=True,
        excludes=list(backup.DEFAULT_EXCLUDES),
    )

    snapshot = destination / "snapshots/2026-08-20"
    assert (snapshot / "root_md/README.md").read_text() == "readme"
    assert (snapshot / "workspace/records/evidence.md").read_text() == "evidence"
    assert not (snapshot / "workspace/records/.env").exists()
    assert result["fileCount"] == 2
    assert json.loads((destination / "latest.json").read_text())["fileCount"] == 2


def test_snapshot_refuses_to_overwrite_existing_day(tmp_path: Path):
    workspace = tmp_path / "workspace"
    destination = tmp_path / "backup"
    workspace.mkdir()
    (destination / "snapshots/2026-08-20").mkdir(parents=True)
    try:
        backup.build_snapshot(workspace, destination, "2026-08-20", [], root_markdown=False, excludes=[])
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected FileExistsError")


def test_excludes_dependency_and_environment_files(tmp_path: Path):
    source = tmp_path / "project"
    (source / "node_modules/pkg").mkdir(parents=True)
    (source / "node_modules/pkg/index.js").write_text("generated")
    (source / ".env").write_text("secret")
    (source / "config/source-map.json").parent.mkdir(parents=True)
    (source / "config/source-map.json").write_text("{}")
    files = list(backup.iter_files(source, backup.DEFAULT_EXCLUDES))
    assert [path.relative_to(source).as_posix() for path in files] == ["config/source-map.json"]
