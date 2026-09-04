import importlib.util
import json
import os
from pathlib import Path

import pytest


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
    latest = json.loads((destination / "latest.json").read_text())
    assert latest["fileCount"] == 2
    assert latest["verified"] is True
    assert latest["restoreCanary"] is True
    verified = backup.verify_snapshot(snapshot)
    assert verified["schema"] == backup.SNAPSHOT_SCHEMA
    assert {row["path"] for row in verified["files"]} == {
        "root_md/README.md", "workspace/records/evidence.md"
    }
    assert backup.restore_canary(snapshot)["temporaryIsolated"] is True


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


def snapshot_fixture(tmp_path: Path):
    workspace = tmp_path / "workspace"
    destination = tmp_path / "backup"
    (workspace / "records/empty").mkdir(parents=True)
    (workspace / "records/item.txt").write_text("recovery data\n", encoding="utf-8")
    (workspace / "README.md").write_text("live workspace\n", encoding="utf-8")
    backup.build_snapshot(
        workspace,
        destination,
        "2026-09-04",
        ["records"],
        root_markdown=True,
        excludes=list(backup.DEFAULT_EXCLUDES),
    )
    return workspace, destination, destination / "snapshots/2026-09-04"


@pytest.mark.parametrize(
    "mutation",
    [
        "tamper", "missing_file", "extra_file", "symlink_file", "hardlink_file",
        "special_file", "extra_dir", "missing_dir",
    ],
)
def test_verify_rejects_tamper_missing_extra_and_symlink(
    tmp_path: Path, mutation: str
):
    _workspace, _destination, snapshot = snapshot_fixture(tmp_path)
    item = snapshot / "workspace/records/item.txt"
    empty = snapshot / "workspace/records/empty"
    if mutation == "tamper":
        item.write_text("tampered\n", encoding="utf-8")
    elif mutation == "missing_file":
        item.unlink()
    elif mutation == "extra_file":
        (item.parent / "extra.txt").write_text("extra", encoding="utf-8")
    elif mutation == "symlink_file":
        item.unlink()
        item.symlink_to(snapshot / "root_md/README.md")
    elif mutation == "hardlink_file":
        os.link(item, snapshot.parent / "external-hardlink")
    elif mutation == "special_file":
        item.unlink()
        os.mkfifo(item)
    elif mutation == "extra_dir":
        (item.parent / "extra-empty").mkdir()
    else:
        empty.rmdir()

    with pytest.raises(RuntimeError):
        backup.verify_snapshot(snapshot)


@pytest.mark.parametrize("unsafe", ["../outside", "/absolute", "root_md\\outside"])
def test_verify_rejects_manifest_traversal(tmp_path: Path, unsafe: str):
    _workspace, _destination, snapshot = snapshot_fixture(tmp_path)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = unsafe
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError):
        backup.verify_snapshot(snapshot)


def test_verify_rejects_traversal_in_source_inventory(tmp_path: Path):
    _workspace, _destination, snapshot = snapshot_fixture(tmp_path)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0]["files"][0]["path"] = "../outside"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError):
        backup.verify_snapshot(snapshot)


def test_restore_canary_never_changes_live_workspace(tmp_path: Path):
    workspace, _destination, snapshot = snapshot_fixture(tmp_path)
    live_file = workspace / "records/item.txt"
    before = live_file.read_bytes()

    result = backup.restore_canary(snapshot)

    assert result["ok"] is True
    assert result["temporaryIsolated"] is True
    assert live_file.read_bytes() == before
    assert "restoreTarget" not in result


def test_create_rejects_traversal_and_symlink_source_without_partial_snapshot(
    tmp_path: Path
):
    workspace = tmp_path / "workspace"
    destination = tmp_path / "backup"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError):
        backup.build_snapshot(
            workspace,
            destination,
            "2026-09-04",
            ["../outside"],
            root_markdown=False,
            excludes=[],
        )
    with pytest.raises(RuntimeError):
        backup.build_snapshot(
            workspace,
            destination,
            "2026-09-04",
            ["linked"],
            root_markdown=False,
            excludes=[],
        )

    assert not (destination / "snapshots/2026-09-04").exists()
    assert list((destination / "snapshots").glob(".2026-09-04-stage-*")) == []


@pytest.mark.parametrize("include", ["/absolute", "../outside", "records/../outside"])
def test_create_rejects_absolute_or_traversing_include(tmp_path: Path, include: str):
    workspace = tmp_path / "workspace"
    destination = tmp_path / "backup"
    workspace.mkdir()

    with pytest.raises(RuntimeError):
        backup.build_snapshot(
            workspace,
            destination,
            "2026-09-04",
            [include],
            root_markdown=False,
            excludes=[],
        )

    assert not (destination / "snapshots/2026-09-04").exists()


def test_create_rejects_missing_duplicate_and_broken_symlink_includes(tmp_path: Path):
    workspace = tmp_path / "workspace"
    destination = tmp_path / "backup"
    (workspace / "records").mkdir(parents=True)

    for includes in (["missing"], ["records", "records"]):
        with pytest.raises(RuntimeError):
            backup.build_snapshot(
                workspace,
                destination,
                "2026-09-04",
                list(includes),
                root_markdown=False,
                excludes=[],
            )

    (workspace / "broken").symlink_to(workspace / "does-not-exist")
    with pytest.raises(RuntimeError, match="symlinked"):
        backup.build_snapshot(
            workspace,
            destination,
            "2026-09-04",
            ["broken"],
            root_markdown=False,
            excludes=[],
        )


def test_create_rejects_parent_child_include_overlap(tmp_path: Path):
    workspace = tmp_path / "workspace"
    destination = tmp_path / "backup"
    (workspace / "memory/sub").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="may not overlap"):
        backup.build_snapshot(
            workspace, destination, "2026-09-04", ["memory", "memory/sub"],
            root_markdown=False, excludes=[],
        )


def test_create_rejects_hardlinked_source(tmp_path: Path):
    workspace = tmp_path / "workspace"
    destination = tmp_path / "backup"
    (workspace / "records").mkdir(parents=True)
    source = workspace / "records/item.txt"
    source.write_text("data", encoding="utf-8")
    os.link(source, tmp_path / "outside-hardlink")
    with pytest.raises(RuntimeError, match="regular|single-link"):
        backup.build_snapshot(
            workspace, destination, "2026-09-04", ["records"],
            root_markdown=False, excludes=[],
        )


def test_atomic_manifest_ignores_predictable_symlink_temp_attack(tmp_path: Path):
    target = tmp_path / "latest.json"
    outside = tmp_path / "outside.json"
    outside.write_text("do not change", encoding="utf-8")
    predictable = tmp_path / "latest.json.tmp-workspace-snapshot"
    predictable.symlink_to(outside)
    backup.atomic_json(target, {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert outside.read_text(encoding="utf-8") == "do not change"


@pytest.mark.parametrize("destination_inside", [True, False])
def test_create_rejects_workspace_destination_overlap(
    tmp_path: Path, destination_inside: bool
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if destination_inside:
        destination = workspace / "backups"
    else:
        destination = tmp_path

    with pytest.raises(RuntimeError, match="must not overlap"):
        backup.build_snapshot(
            workspace,
            destination,
            "2026-09-04",
            [],
            root_markdown=False,
            excludes=[],
        )


def test_existing_snapshot_is_immutable_even_when_invalid(tmp_path: Path):
    workspace = tmp_path / "workspace"
    destination = tmp_path / "backup"
    workspace.mkdir()
    existing = destination / "snapshots/2026-09-04"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("do not replace", encoding="utf-8")

    with pytest.raises(FileExistsError):
        backup.build_snapshot(
            workspace,
            destination,
            "2026-09-04",
            [],
            root_markdown=False,
            excludes=[],
        )

    assert marker.read_text(encoding="utf-8") == "do not replace"


def test_verify_and_restore_canary_cli_operations(tmp_path: Path, capsys, monkeypatch):
    _workspace, _destination, snapshot = snapshot_fixture(tmp_path)
    monkeypatch.setattr("sys.argv", ["backup_workspace_assets.py", "verify", "--snapshot", str(snapshot)])
    assert backup.main() == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    monkeypatch.setattr("sys.argv", ["backup_workspace_assets.py", "restore-canary", "--snapshot", str(snapshot)])
    assert backup.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["temporaryIsolated"] is True


def test_snapshot_root_symlink_is_rejected(tmp_path: Path):
    _workspace, _destination, snapshot = snapshot_fixture(tmp_path)
    link = tmp_path / "snapshot-link"
    link.symlink_to(snapshot, target_is_directory=True)

    with pytest.raises(RuntimeError):
        backup.verify_snapshot(link)


def test_latest_pointer_is_not_updated_when_restore_canary_fails(
    tmp_path: Path, monkeypatch
):
    workspace = tmp_path / "workspace"
    destination = tmp_path / "backup"
    (workspace / "records").mkdir(parents=True)
    (workspace / "records/item.txt").write_text("data", encoding="utf-8")
    monkeypatch.setattr(
        backup,
        "restore_canary",
        lambda _snapshot: (_ for _ in ()).throw(RuntimeError("injected canary failure")),
    )

    with pytest.raises(RuntimeError, match="injected canary failure"):
        backup.build_snapshot(
            workspace,
            destination,
            "2026-09-04",
            ["records"],
            root_markdown=False,
            excludes=[],
        )

    assert not (destination / "latest.json").exists()
    # The atomically published snapshot is retained for diagnosis but is never
    # advertised as the latest valid recovery point.
    assert (destination / "snapshots/2026-09-04").is_dir()


def test_dry_run_validates_missing_include_instead_of_claiming_readiness(
    tmp_path: Path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        [
            "backup_workspace_assets.py",
            "create",
            "--workspace",
            str(workspace),
            "--destination",
            str(tmp_path / "backup"),
            "--include",
            "missing",
        ],
    )

    with pytest.raises(RuntimeError, match="configured snapshot include is missing"):
        backup.main()
