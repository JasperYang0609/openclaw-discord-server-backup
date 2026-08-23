import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/audit_cron_tooling.py"
spec = importlib.util.spec_from_file_location("audit_cron_tooling", SCRIPT)
cron = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(cron)


def test_empty_tools_allow_is_still_rejected():
    result = cron.audit_jobs({"jobs": [{
        "id": "job-1",
        "name": "snapshot",
        "enabled": True,
        "payload": {"model": "openai/gpt-5.5", "message": "npm run snapshot:backup", "toolsAllow": []},
    }]})
    assert result["ok"] is False
    assert result["legacyToolsAllow"] == 1
    assert result["findings"][0]["remediation"].endswith("job-1 --clear-tools")
    assert result["canary"]["command"] == "pwd && echo TOOL_OK"


def test_absent_tools_allow_passes():
    result = cron.audit_jobs({"jobs": [{
        "id": "job-2",
        "enabled": True,
        "payload": {"model": "openai/gpt-5.5", "message": "python3 scripts/check.py"},
    }]})
    assert result["ok"] is True
    assert result["legacyToolsAllow"] == 0
