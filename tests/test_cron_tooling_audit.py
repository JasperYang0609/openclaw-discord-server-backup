import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skill/openclaw-discord-server-backup/scripts/audit_cron_tooling.py"
spec = importlib.util.spec_from_file_location("audit_cron_tooling", SCRIPT)
cron = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(cron)


def test_command_tools_allow_is_rejected_with_transactional_remediation():
    result = cron.audit_jobs({"jobs": [{
        "id": "job-1",
        "name": "snapshot",
        "enabled": True,
        "payload": {"kind": "command", "argv": ["npm", "run", "snapshot:backup"], "toolsAllow": []},
    }]})
    assert result["ok"] is False
    assert result["legacyToolsAllow"] == 1
    assert result["findings"][0]["payloadKind"] == "command"
    assert result["findings"][0]["remediationMode"] == "transactional_recreate"
    assert "--clear-tools" not in result["findings"][0]["remediation"]
    assert " --tools" not in result["findings"][0]["remediation"]
    assert result["canary"]["command"] == "pwd && echo TOOL_OK"


def test_agent_turn_tools_allow_uses_supported_clear_remediation():
    result = cron.audit_jobs({"jobs": [{
        "id": "job-agent",
        "name": "daily-sync",
        "enabled": True,
        "payload": {"kind": "agentTurn", "message": "python3 scripts/check.py", "toolsAllow": []},
    }]})
    finding = result["findings"][0]
    assert finding["payloadKind"] == "agentTurn"
    assert finding["remediationMode"] == "agent_turn_clear"
    assert finding["remediation"].endswith("job-agent --clear-tools")


def test_unknown_payload_kind_never_guesses_clear_tools_remediation():
    result = cron.audit_jobs({"jobs": [{
        "id": "job-unknown",
        "enabled": True,
        "payload": {"message": "python3 scripts/check.py", "toolsAllow": []},
    }]})
    finding = result["findings"][0]
    assert finding["payloadKind"] == "unknown"
    assert finding["remediationMode"] == "manual_review"
    assert "--clear-tools" not in finding["remediation"]


def test_absent_tools_allow_passes():
    result = cron.audit_jobs({"jobs": [{
        "id": "job-2",
        "enabled": True,
        "payload": {"model": "openai/gpt-5.5", "message": "python3 scripts/check.py"},
    }]})
    assert result["ok"] is True
    assert result["legacyToolsAllow"] == 0
