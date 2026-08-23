import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/snapshot_deployment_assets.py"
spec = importlib.util.spec_from_file_location("snapshot_deployment_assets", SCRIPT)
recovery = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(recovery)


def test_bundle_includes_required_custom_assets_and_restore_canary(tmp_path: Path):
    adapter = tmp_path / "adapter.py"
    ledger = tmp_path / "mapping.json"
    adapter.write_text("print('adapter')\n", encoding="utf-8")
    ledger.write_text("{}\n", encoding="utf-8")
    bundle = tmp_path / "snapshot"

    manifest = recovery.create_bundle(
        bundle,
        {"inventory_adapter": adapter, "mapping_ledger": ledger},
        {"inventory_adapter", "mapping_ledger"},
    )

    assert manifest["files"] == 2
    assert recovery.verify_bundle(bundle)["ok"] is True
    assert recovery.restore_canary(bundle)["ok"] is True


def test_bundle_rejects_missing_required_asset(tmp_path: Path):
    try:
        recovery.create_bundle(tmp_path / "snapshot", {}, {"inventory_adapter"})
        raised = False
    except RuntimeError as exc:
        raised = "required recovery assets missing" in str(exc)
    assert raised
