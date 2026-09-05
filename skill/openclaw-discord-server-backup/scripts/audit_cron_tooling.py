#!/usr/bin/env python3
"""Detect legacy cron tool allowlists and emit payload-kind-safe remediation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SHELL_MARKERS = (
    "python3 ", "npm ", "node ", "bash ", "sh ", "pwd", "openclaw ",
    "scripts/", "--state", "--queue", "snapshot", "reconcile",
)


def job_name(job: dict[str, Any]) -> str:
    return str(job.get("name") or job.get("id") or "unnamed")


def payload(job: dict[str, Any]) -> dict[str, Any]:
    value = job.get("payload")
    return value if isinstance(value, dict) else {}


def audit_jobs(data: Any) -> dict[str, Any]:
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise ValueError("cron JSON must be a job array or an object with jobs[]")
    findings: list[dict[str, Any]] = []
    enabled = [job for job in jobs if job.get("enabled", True)]
    for job in enabled:
        job_payload = payload(job)
        payload_kind = str(job_payload.get("kind") or "unknown")
        argv = job_payload.get("argv") if isinstance(job_payload.get("argv"), list) else []
        message = " ".join([
            str(job_payload.get("message") or job_payload.get("prompt") or ""),
            *(str(item) for item in argv),
        ])
        model = str(job_payload.get("model") or job.get("model") or "")
        has_legacy_field = "toolsAllow" in job_payload
        shell_risk = any(marker in message for marker in SHELL_MARKERS)
        if has_legacy_field:
            if payload_kind == "agentTurn":
                remediation_mode = "agent_turn_clear"
                remediation = f"openclaw cron edit {job.get('id')} --clear-tools"
            elif payload_kind == "command":
                remediation_mode = "transactional_recreate"
                remediation = (
                    "fail closed and transactionally recreate this command job from its approved declaration; "
                    "do not apply tool-list edits to command jobs"
                )
            else:
                remediation_mode = "manual_review"
                remediation = (
                    "fail closed until the payload kind is verified; do not guess a tools edit operation"
                )
            findings.append({
                "jobId": str(job.get("id") or ""),
                "name": job_name(job),
                "model": model,
                "payloadKind": payload_kind,
                "shellRequired": shell_risk,
                "code": "LEGACY_PAYLOAD_TOOLS_ALLOW",
                "remediationMode": remediation_mode,
                "remediation": remediation,
            })
    return {
        "schema": "openclaw-cron-tooling-audit-v1",
        "ok": not findings,
        "jobs": len(jobs),
        "enabledJobs": len(enabled),
        "legacyToolsAllow": len(findings),
        "findings": findings,
        "canary": {
            "requiredAfterRepair": bool(findings),
            "sessionTarget": "isolated",
            "payloadToolsAllowMustBeAbsent": True,
            "command": "pwd && echo TOOL_OK",
            "successMarker": "TOOL_OK",
            "cleanup": "remove the temporary canary job after a successful run",
        },
        "rule": (
            "toolsAllow: [] is still legacy configuration. Only agentTurn jobs may use --clear-tools; "
            "command jobs must fail closed and be transactionally recreated without toolsAllow."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit OpenClaw cron JSON for legacy payload.toolsAllow.")
    parser.add_argument("--input", help="cron list --all --json output; defaults to stdin")
    parser.add_argument("--out")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    raw = Path(args.input).read_text(encoding="utf-8") if args.input else sys.stdin.read()
    result = audit_jobs(json.loads(raw))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    if args.compact:
        print(
            f"[cron-tooling] ok={str(result['ok']).lower()} enabled={result['enabledJobs']} "
            f"legacyToolsAllow={result['legacyToolsAllow']}"
        )
    else:
        print(text)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
