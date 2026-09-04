#!/usr/bin/env python3
"""Declaratively install, upgrade, verify, and roll back owned backup cron jobs.

Only jobs with an exact declaration key from the packaged manifest are owned.
Unknown jobs are never changed; look-alikes block activation for human review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA = "openclaw-owned-cron-manifest.v1"
RECEIPT_SCHEMA = "openclaw-cron-reconciliation-receipt.v1"
MAX_RECEIPT_BYTES = 512 * 1024
MAX_DIGEST_BYTES = 256
ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
GUILD_RE = re.compile(r"^[0-9]{6,32}$")
SAFE_STATUSES = {"create", "update", "unchanged"}
LEGACY_ROLE_TOKENS = {
    "core-backup": {"core_workspace_backup.py"},
    "discovery": {"audit_discord_inventory_v3.py", "discovery.md"},
    "daily-sync-1": {"daily-sync-v3.md", "check_daily_sync_gate.py"},
    "daily-sync-2": {"daily-sync-v3.md", "check_daily_sync_gate.py"},
    "daily-sync-3": {"daily-sync-v3.md", "check_daily_sync_gate.py"},
    "caught-up-audit": {"audit_caught_up_v3.py"},
    "backlog": {"run_backlog_worker_v3.py", "backlog-worker-v3.md"},
    "weekly-inventory": {"audit_discord_inventory_v3.py"},
    "weekly-raw": {"weekly_raw_reconcile_v4.py"},
    "workspace-snapshot": {"backup_workspace_assets.py"},
    "health-report": {"backup_health_report.py"},
}
LEGACY_DECLARATION_KEYS = {
    "weekly-inventory": {"discord-inventory-weekly-v1"},
    "weekly-raw": {"discord-raw-integrity-weekly-v1"},
    "workspace-snapshot": {"workspace-critical-assets-monthly-v1"},
}
LEGACY_PAYLOAD_KINDS = {
    "backlog": {"agentTurn", "command"},
}


class CronManagerError(RuntimeError):
    pass


class CronRollbackIncompleteError(CronManagerError):
    pass


@dataclass(frozen=True)
class RenderContext:
    workspace: Path
    skill_dir: Path
    config_path: Path
    backup_root: Path
    guild_id: str
    report_to: str
    agent: str
    timezone_name: str
    receipt_dir: Path
    account_id: str | None = None
    python_executable: str = "python3"
    openclaw_binary: str = "openclaw"


def canonical_json(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    secure_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CronManagerError(f"unreadable JSON: {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise CronManagerError(f"JSON root must be an object: {path.name}")
    return data


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != SCHEMA:
        raise CronManagerError("unsupported cron manifest schema")
    product = manifest.get("product")
    contract = manifest.get("contractVersion")
    jobs = manifest.get("jobs")
    alert = manifest.get("failureAlert")
    if not isinstance(product, str) or not ROLE_RE.fullmatch(product):
        raise CronManagerError("manifest product is invalid")
    if not isinstance(contract, str) or not ROLE_RE.fullmatch(contract):
        raise CronManagerError("manifest contractVersion is invalid")
    if not isinstance(jobs, list) or not jobs:
        raise CronManagerError("manifest jobs must be a non-empty array")
    if not isinstance(alert, dict) or alert.get("after") != 1 or alert.get("includeSkipped") is not False:
        raise CronManagerError("manifest failure alert must trigger after one error and exclude safe skips")
    roles: set[str] = set()
    for row in jobs:
        if not isinstance(row, dict):
            raise CronManagerError("manifest job must be an object")
        role = row.get("role")
        if not isinstance(role, str) or not ROLE_RE.fullmatch(role) or role in roles:
            raise CronManagerError(f"duplicate or invalid manifest role: {role}")
        roles.add(role)
        if row.get("kind") not in {"command", "agent"}:
            raise CronManagerError(f"unsupported job kind for {role}")
        if not isinstance(row.get("schedule"), str) or len(row["schedule"].split()) not in {5, 6}:
            raise CronManagerError(f"invalid cron schedule for {role}")
        if row.get("delivery") not in {"none", "announce"}:
            raise CronManagerError(f"invalid delivery for {role}")
        if row.get("kind") == "command" and not ROLE_RE.fullmatch(str(row.get("runnerRole") or "")):
            raise CronManagerError(f"missing runnerRole for {role}")
        if row.get("kind") == "agent" and not isinstance(row.get("promptFile"), str):
            raise CronManagerError(f"missing promptFile for {role}")
    required = {
        "core-backup", "discovery", "daily-sync-1", "daily-sync-2", "daily-sync-3",
        "caught-up-audit", "backlog", "weekly-inventory", "weekly-raw",
        "workspace-snapshot", "health-report",
    }
    if roles != required:
        raise CronManagerError(f"manifest role set mismatch: missing={sorted(required-roles)} extra={sorted(roles-required)}")
    by_role = {row["role"]: row for row in jobs}
    if by_role["backlog"]["schedule"] != "10 23,0,1,2,3,4 * * *":
        raise CronManagerError("backlog must remain in the night-only window")
    if by_role["health-report"]["schedule"] != "5 7 * * *":
        raise CronManagerError("health report must run at 07:05")
    if by_role["workspace-snapshot"]["schedule"] != "0 7 1 * *":
        raise CronManagerError("workspace recovery snapshot must remain monthly at 07:00 on day one")
    sessions = {by_role[f"daily-sync-{index}"].get("sharedSession") for index in (1, 2, 3)}
    if sessions != {"daily-sync"}:
        raise CronManagerError("all daily-sync jobs must share the daily-sync session")


def ensure_absolute_safe(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise CronManagerError(f"{label} must be absolute")
    reject_symlink_components(path, label=label)
    normalized = path.resolve(strict=False)
    if normalized == Path(normalized.anchor):
        raise CronManagerError(f"{label} must not be a filesystem root")
    return normalized


def trusted_executable(value: str, *, label: str) -> str:
    candidate = Path(value).expanduser() if Path(value).is_absolute() else Path(shutil.which(value) or "")
    if not str(candidate):
        raise CronManagerError(f"{label} executable was not found")
    try:
        resolved = candidate.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise CronManagerError(f"{label} executable is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()} or stat.S_IMODE(info.st_mode) & 0o022:
        raise CronManagerError(f"{label} executable is not safely owned or is writable by another user")
    if not os.access(resolved, os.X_OK):
        raise CronManagerError(f"{label} executable is not executable")
    return str(resolved)


def reject_symlink_components(path: Path, *, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise CronManagerError(f"{label} contains a symlinked path component")


def secure_directory(path: Path) -> None:
    reject_symlink_components(path, label="managed receipt path")
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise CronManagerError("managed receipt parent is not a safe directory")
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def workspace_child(workspace: Path, value: str, *, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = Path(os.path.abspath(candidate))
    reject_symlink_components(candidate, label=label)
    candidate = candidate.resolve(strict=False)
    if candidate != workspace and workspace not in candidate.parents:
        raise CronManagerError(f"{label} must stay inside workspace")
    return candidate


def declaration_key(product: str, guild_id: str, role: str, contract: str) -> str:
    return f"{product}:{guild_id}:{role}:{contract}"


def render_prompt(path: Path, replacements: dict[str, str]) -> str:
    if path.is_symlink() or not path.is_file():
        raise CronManagerError(f"prompt is missing or unsafe: {path.name}")
    text = path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", text)))
    if unresolved:
        raise CronManagerError(f"prompt has unresolved placeholders: {unresolved}")
    return text


def render_jobs(manifest: dict[str, Any], context: RenderContext) -> list[dict[str, Any]]:
    validate_manifest(manifest)
    if not GUILD_RE.fullmatch(context.guild_id):
        raise CronManagerError("guild ID must be a numeric Discord ID")
    workspace = ensure_absolute_safe(context.workspace, label="workspace")
    skill_dir = ensure_absolute_safe(context.skill_dir, label="skill directory")
    config_path = workspace_child(workspace, str(context.config_path), label="config path")
    receipt_dir = workspace_child(workspace, str(context.receipt_dir), label="receipt directory")
    backup_root = ensure_absolute_safe(context.backup_root, label="backup root")
    if workspace == backup_root or workspace in backup_root.parents or backup_root in workspace.parents:
        raise CronManagerError("backup root and workspace must not overlap")
    report_to = context.report_to.removeprefix("discord:")
    if not re.fullmatch(r"(?:channel|user):[0-9]{6,32}", report_to):
        raise CronManagerError("report destination must be an explicit Discord channel:ID or user:ID")
    try:
        ZoneInfo(context.timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise CronManagerError("timezone must be a valid IANA timezone") from exc
    if not context.agent.strip() or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", context.agent):
        raise CronManagerError("report destination, agent, and timezone are required")
    for path, label in (
        (workspace, "workspace"), (skill_dir, "skill directory"),
        (config_path, "config path"), (receipt_dir, "receipt directory"),
        (backup_root, "backup root"),
    ):
        reject_symlink_components(path, label=label)

    config = read_json(config_path)
    state_path = workspace_child(workspace, str(config.get("statePath") or ""), label="state path")
    queue_path = workspace_child(workspace, str(config.get("queuePath") or ""), label="queue path")
    reports = receipt_dir / "reports"
    inventory_report = reports / "daily-inventory.json"
    mapping_ledger = reports / "inventory-mapping.json"
    evidence_root = backup_root.parent / "備份驗證證據"
    replacements = {
        "WORKSPACE_ROOT": str(workspace),
        "SKILL_ROOT": str(skill_dir),
        "CONFIG_PATH": str(config_path),
        "STATE_PATH": str(state_path),
        "QUEUE_PATH": str(queue_path),
        "BACKUP_ROOT": str(backup_root.parent),
        "DISCORD_ROOT": str(backup_root),
        "INVENTORY_REPORT": str(inventory_report),
        "RECEIPT_DIR": str(receipt_dir),
    }
    shared_session = f"session:{manifest['product']}-{context.guild_id}-daily-sync"
    desired: list[dict[str, Any]] = []
    for row in manifest["jobs"]:
        role = row["role"]
        key = declaration_key(manifest["product"], context.guild_id, role, manifest["contractVersion"])
        payload: dict[str, Any]
        if row["kind"] == "command":
            argv = [
                context.python_executable,
                str(skill_dir / "scripts/run_managed_component.py"),
                "--role", row["runnerRole"],
                "--workspace", str(workspace),
                "--config", str(config_path),
                "--backup-root", str(backup_root.parent),
                "--receipt-dir", str(receipt_dir),
                "--declaration-key", key,
                "--openclaw-bin", context.openclaw_binary,
            ]
            payload = {
                "kind": "command",
                "argv": argv,
                "cwd": str(workspace),
                "timeoutSeconds": int(row["timeoutSeconds"]),
                "noOutputTimeoutSeconds": int(row["noOutputTimeoutSeconds"]),
                "outputMaxBytes": int(row["outputMaxBytes"]),
            }
        else:
            slot_replacements = {**replacements, "COMPONENT": role, "DECLARATION_KEY": key}
            message = render_prompt(skill_dir / row["promptFile"], slot_replacements)
            payload = {
                "kind": "agentTurn",
                "message": message,
                "timeoutSeconds": int(row["timeoutSeconds"]),
                "lightContext": True,
            }
        delivery = {"mode": row["delivery"]}
        if row["delivery"] == "announce":
            delivery.update({"channel": "discord", "to": report_to})
            if context.account_id:
                delivery["accountId"] = context.account_id
        alert = {
            "after": 1,
            "channel": "discord",
            "to": report_to,
            "cooldownMs": 3_600_000,
            "includeSkipped": False,
            "mode": "announce",
        }
        if context.account_id:
            alert["accountId"] = context.account_id
        job = {
            "declarationKey": key,
            "role": role,
            "name": row["name"],
            "description": row["description"],
            "enabled": True,
            "schedule": {
                "kind": "cron",
                "expr": row["schedule"],
                "tz": context.timezone_name,
                "staggerMs": 0,
            },
            "sessionTarget": shared_session if row["kind"] == "agent" else "isolated",
            "payload": payload,
            "delivery": delivery,
            "failureAlert": alert,
        }
        if row["kind"] == "agent":
            job["agentId"] = context.agent
        desired.append(job)
    daily_targets = {
        job["sessionTarget"] for job in desired
        if str(job.get("role") or "").startswith("daily-sync-")
    }
    if len(daily_targets) != 1 or not next(iter(daily_targets)).startswith("session:"):
        raise CronManagerError("daily-sync jobs do not share one persistent session target")
    return desired


def normalized_delivery(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    mode = data.get("mode") or "none"
    result: dict[str, Any] = {"mode": mode}
    if mode != "none":
        for key in ("channel", "to", "accountId", "bestEffort"):
            if key in data:
                result[key] = data[key]
    return result


def normalized_alert(value: Any) -> dict[str, Any] | bool:
    if not value:
        return False
    data = value if isinstance(value, dict) else {}
    result = {
        "after": int(data.get("after") or 0),
        "channel": data.get("channel"),
        "to": data.get("to"),
        "cooldownMs": int(data.get("cooldownMs") or 0),
        "includeSkipped": bool(data.get("includeSkipped")),
        "mode": data.get("mode") or "announce",
    }
    if data.get("accountId") is not None:
        result["accountId"] = data.get("accountId")
    return result


def job_contract(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    payload_contract = {
        key: payload.get(key)
        for key in (
            "kind", "argv", "cwd", "message", "timeoutSeconds",
            "noOutputTimeoutSeconds", "outputMaxBytes", "lightContext",
            "toolsAllow",
        )
        if key in payload
    }
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    schedule_contract = {
        key: schedule.get(key)
        for key in ("kind", "expr", "tz", "staggerMs")
        if key in schedule
    }
    result: dict[str, Any] = {
        "declarationKey": job.get("declarationKey"),
        "name": job.get("name"),
        "description": job.get("description"),
        "enabled": bool(job.get("enabled")),
        "schedule": schedule_contract,
        "sessionTarget": job.get("sessionTarget"),
        "payload": payload_contract,
        "delivery": normalized_delivery(job.get("delivery")),
        "failureAlert": normalized_alert(job.get("failureAlert")),
    }
    if payload.get("kind") == "agentTurn":
        result["agentId"] = job.get("agentId")
    return result


def validate_restorable_owned_jobs(inventory: list[dict[str, Any]], desired: list[dict[str, Any]]) -> None:
    owned_keys = {str(job["declarationKey"]) for job in desired}
    for job in inventory:
        if str(job.get("declarationKey") or "") not in owned_keys:
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        # Environment values may contain secrets and are not persisted into a
        # rollback receipt. Refuse the upgrade before any mutation instead of
        # silently dropping or exposing them.
        if "env" in payload:
            raise CronManagerError("owned job contains an environment field that cannot be safely rollback-persisted")
        if "toolsAllow" in payload:
            tools = payload["toolsAllow"]
            if (
                payload.get("kind") != "agentTurn"
                or not isinstance(tools, list)
                or any(not isinstance(item, str) or not item.strip() for item in tools)
            ):
                raise CronManagerError("owned job has an unrestorable tools policy")


def collision_tokens(desired: Iterable[dict[str, Any]]) -> tuple[set[str], set[str]]:
    script_tokens: set[str] = set()
    session_tokens: set[str] = set()
    for job in desired:
        payload = job["payload"]
        if payload["kind"] == "command":
            for value in payload.get("argv") or []:
                text = str(value)
                if text.endswith(".py"):
                    script_tokens.add(Path(text).name)
        else:
            script_tokens.add("daily-sync-v3.md")
        if str(job.get("sessionTarget") or "").startswith("session:"):
            session_tokens.add(str(job["sessionTarget"]))
        script_tokens.update(LEGACY_ROLE_TOKENS.get(str(job.get("role") or ""), set()))
    return script_tokens, session_tokens


def job_haystack(job: dict[str, Any]) -> str:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    return " ".join(str(value) for value in (payload.get("argv") or [])) + " " + str(payload.get("message") or "")


def job_fingerprint(job: dict[str, Any]) -> str:
    payload = canonical_json(redacted_job(job))
    return sha256_bytes(payload)


def rollback_unknown_originals(
    inventory: Iterable[dict[str, Any]],
    *,
    managed_keys: Iterable[str],
    managed_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Return pre-existing jobs that rollback is not authorized to mutate."""
    key_set = {str(key) for key in managed_keys if key is not None and str(key)}
    id_set = {str(job_id) for job_id in managed_ids if job_id is not None and str(job_id)}
    return [
        job for job in inventory
        if str(job.get("id") or "") not in id_set
        and str(job.get("declarationKey") or "") not in key_set
    ]


def rollback_verification_errors(
    inventory: Iterable[dict[str, Any]],
    *,
    owned_originals: Iterable[dict[str, Any]],
    adopted_originals: Iterable[dict[str, Any]],
    created_keys: Iterable[str],
    unknown_originals: Iterable[dict[str, Any]],
) -> list[str]:
    """Verify the observable cron topology after rollback.

    Command success is not evidence that durable cron state changed.  This
    verifier reads the inventory back and compares every pre-change contract
    that the transaction either owned or was required to preserve.
    """
    rows = list(inventory)
    owned = list(owned_originals)
    adopted = list(adopted_originals)
    unknown = list(unknown_originals)
    errors: list[str] = []
    by_id: dict[str, list[dict[str, Any]]] = {}
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        job_id = str(row.get("id") or "")
        key = str(row.get("declarationKey") or "")
        if job_id:
            by_id.setdefault(job_id, []).append(row)
        if key:
            by_key.setdefault(key, []).append(row)
    for job_id, matches in by_id.items():
        if len(matches) > 1:
            errors.append(f"verify-duplicate-id:{job_id}")
    for key, matches in by_key.items():
        if len(matches) > 1:
            errors.append(f"verify-duplicate-key:{key}")

    for key in sorted({str(value) for value in created_keys if str(value)}):
        if by_key.get(key):
            errors.append(f"verify-created:{key}:still-present")

    seen_owned_keys: set[str] = set()
    for original in owned:
        key = str(original.get("declarationKey") or "")
        original_id = str(original.get("id") or "")
        if not key or key in seen_owned_keys:
            errors.append(f"verify-owned:{key or 'missing-key'}:invalid-original")
            continue
        seen_owned_keys.add(key)
        matches = by_key.get(key, [])
        if len(matches) != 1:
            errors.append(f"verify-owned:{key}:missing-or-duplicate")
            continue
        actual = matches[0]
        if str(actual.get("id") or "") != original_id or job_contract(actual) != job_contract(original):
            errors.append(f"verify-owned:{key}:contract-mismatch")

    seen_adopted_ids: set[str] = set()
    for original in adopted:
        original_id = str(original.get("id") or "")
        if not original_id or original_id in seen_adopted_ids:
            errors.append(f"verify-adopted:{original_id or 'missing-id'}:invalid-original")
            continue
        seen_adopted_ids.add(original_id)
        matches = by_id.get(original_id, [])
        if len(matches) != 1:
            errors.append(f"verify-adopted:{original_id}:missing-or-duplicate")
            continue
        if job_contract(matches[0]) != job_contract(original):
            errors.append(f"verify-adopted:{original_id}:contract-mismatch")

    seen_unknown_ids: set[str] = set()
    for original in unknown:
        original_id = str(original.get("id") or "")
        if not original_id or original_id in seen_unknown_ids:
            errors.append(f"verify-unknown:{original_id or 'missing-id'}:invalid-original")
            continue
        seen_unknown_ids.add(original_id)
        matches = by_id.get(original_id, [])
        if len(matches) != 1:
            errors.append(f"verify-unknown:{original_id}:missing-or-duplicate")
            continue
        if job_fingerprint(matches[0]) != job_fingerprint(original):
            errors.append(f"verify-unknown:{original_id}:contract-mismatch")
    return errors


def unknown_collisions(
    inventory: list[dict[str, Any]],
    desired: list[dict[str, Any]],
    *,
    ignored_job_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    owned_keys = {job["declarationKey"] for job in desired}
    scripts, sessions = collision_tokens(desired)
    desired_names = {str(job.get("name") or "") for job in desired}
    desired_schedules = {
        (str((job.get("schedule") or {}).get("expr") or ""), str((job.get("schedule") or {}).get("tz") or ""))
        for job in desired
    }
    report_schedules = {
        (
            str((job.get("schedule") or {}).get("expr") or ""),
            str((job.get("schedule") or {}).get("tz") or ""),
            str(normalized_delivery(job.get("delivery")).get("channel") or ""),
            str(normalized_delivery(job.get("delivery")).get("to") or ""),
        )
        for job in desired
        if normalized_delivery(job.get("delivery")).get("mode") == "announce"
    }
    ignored_job_ids = ignored_job_ids or set()
    collisions: list[dict[str, str]] = []
    for job in inventory:
        if str(job.get("id") or "") in ignored_job_ids:
            continue
        if job.get("declarationKey") in owned_keys:
            continue
        haystack = job_haystack(job)
        matched = sorted(token for token in scripts if token in haystack)
        same_session = str(job.get("sessionTarget") or "") in sessions
        name = str(job.get("name") or "")
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        schedule_key = (str(schedule.get("expr") or ""), str(schedule.get("tz") or ""))
        delivery = normalized_delivery(job.get("delivery"))
        report_key = (
            schedule_key[0], schedule_key[1],
            str(delivery.get("channel") or ""), str(delivery.get("to") or ""),
        )
        declaration = str(job.get("declarationKey") or "")
        category = ""
        match_text = ""
        if matched:
            category, match_text = "script_collision", ",".join(matched)
        elif same_session:
            category, match_text = "session_collision", "shared_daily_session"
        elif name in desired_names:
            category, match_text = "managed_name_collision", name
        elif name.startswith("Discord 備份｜") and schedule_key in desired_schedules:
            category, match_text = "managed_schedule_collision", schedule_key[0]
        elif delivery.get("mode") == "announce" and report_key in report_schedules:
            category, match_text = "report_schedule_collision", f"{schedule_key[0]}:{delivery.get('to')}"
        elif declaration.startswith("openclaw-discord-server-backup:"):
            category, match_text = "managed_declaration_collision", declaration
        if category:
            collisions.append({
                "jobId": str(job.get("id") or "unknown"),
                "category": category,
                "match": match_text,
            })
    return collisions


def validate_inventory(payload: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise CronManagerError("cron inventory schema is invalid")
    if payload.get("hasMore") not in {False, None}:
        raise CronManagerError("cron inventory is incomplete (hasMore=true)")
    total = payload.get("total")
    if not isinstance(total, int) or total != len(jobs):
        raise CronManagerError("cron inventory count does not match the returned jobs")
    if payload.get("offset") not in {0, None}:
        raise CronManagerError("cron inventory did not start at offset zero")
    ids = [str(job.get("id") or "") for job in jobs]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise CronManagerError("cron inventory contains missing or duplicate job IDs")
    keys = [str(job.get("declarationKey")) for job in jobs if job.get("declarationKey")]
    if len(keys) != len(set(keys)):
        raise CronManagerError("cron inventory contains duplicate declaration keys")
    return jobs


def build_plan(
    inventory: list[dict[str, Any]],
    desired: list[dict[str, Any]],
    *,
    adopted_job_ids: set[str] | None = None,
) -> dict[str, Any]:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for job in inventory:
        key = job.get("declarationKey")
        if key:
            by_key.setdefault(str(key), []).append(job)
    duplicate_keys = sorted(key for key, rows in by_key.items() if len(rows) > 1 and key in {item["declarationKey"] for item in desired})
    actions: list[dict[str, Any]] = []
    for expected in desired:
        matches = by_key.get(expected["declarationKey"], [])
        if not matches:
            actions.append({"role": expected["role"], "declarationKey": expected["declarationKey"], "action": "create", "jobId": None})
        elif len(matches) == 1:
            current = matches[0]
            action = "unchanged" if job_contract(current) == job_contract(expected) and "toolsAllow" not in (current.get("payload") or {}) else "update"
            actions.append({"role": expected["role"], "declarationKey": expected["declarationKey"], "action": action, "jobId": str(current["id"])})
    collisions = unknown_collisions(inventory, desired, ignored_job_ids=adopted_job_ids)
    return {
        "ok": not duplicate_keys and not collisions,
        "actions": actions,
        "duplicateOwnedKeys": duplicate_keys,
        "unknownCollisions": collisions,
        "summary": {status: sum(row["action"] == status for row in actions) for status in SAFE_STATUSES},
    }


def validate_adoption_map(
    path: Path | None,
    inventory: list[dict[str, Any]],
    desired: list[dict[str, Any]],
    guild_id: str,
) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    data = read_json(path)
    if data.get("schema") != "openclaw-cron-adoption-map.v1" or str(data.get("guildId")) != guild_id:
        raise CronManagerError("adoption map schema or guild ID is invalid")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise CronManagerError("adoption map entries must be a non-empty array")
    jobs_by_id = {str(job.get("id") or ""): job for job in inventory}
    desired_roles = {job["role"] for job in desired}
    desired_by_role = {job["role"]: job for job in desired}
    adopted: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for row in entries:
        if not isinstance(row, dict):
            raise CronManagerError("adoption entry must be an object")
        role = str(row.get("role") or "")
        job_id = str(row.get("jobId") or "")
        expected = str(row.get("jobSha256") or "")
        if role not in desired_roles or role in adopted or not job_id or job_id in seen_ids:
            raise CronManagerError("adoption entry role/job ID is invalid or duplicated")
        job = jobs_by_id.get(job_id)
        if job is None:
            raise CronManagerError(f"adoption job does not exist: {job_id}")
        legacy_key = job.get("declarationKey")
        if legacy_key and str(legacy_key) not in LEGACY_DECLARATION_KEYS.get(role, set()):
            raise CronManagerError("adoption declaration key is not in the hardcoded legacy allowlist")
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or job_fingerprint(job) != expected:
            raise CronManagerError(f"adoption fingerprint mismatch for role {role}")
        tokens = LEGACY_ROLE_TOKENS.get(role, set())
        if not any(token in job_haystack(job) for token in tokens):
            raise CronManagerError(f"adoption payload does not match the declared role {role}")
        expected_job = desired_by_role[role]
        actual_schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        expected_schedule = expected_job["schedule"]
        if (
            actual_schedule.get("kind") != "cron"
            or actual_schedule.get("expr") != expected_schedule.get("expr")
            or actual_schedule.get("tz") != expected_schedule.get("tz")
        ):
            raise CronManagerError(f"adoption schedule/timezone mismatch for role {role}")
        actual_kind = (job.get("payload") or {}).get("kind") if isinstance(job.get("payload"), dict) else None
        allowed_kinds = LEGACY_PAYLOAD_KINDS.get(role, {expected_job["payload"]["kind"]})
        if actual_kind not in allowed_kinds:
            raise CronManagerError(f"adoption payload kind mismatch for role {role}")
        adopted[role] = job
        seen_ids.add(job_id)
    return adopted


def validate_prepared_adoption(
    receipt_path: Path,
    adoption_map: Path,
    inventory: list[dict[str, Any]],
    desired: list[dict[str, Any]],
    guild_id: str,
) -> dict[str, dict[str, Any]]:
    receipt, rollback_data = load_verified_receipt(receipt_path)
    if receipt.get("schema") != RECEIPT_SCHEMA or rollback_data.get("schema") != "openclaw-cron-rollback-contract.v1":
        raise CronManagerError("prepared adoption receipt schema is invalid")
    plan = receipt.get("plan") if isinstance(receipt.get("plan"), dict) else {}
    receipt_adopted = plan.get("adopted") if isinstance(plan.get("adopted"), list) else []
    originals = rollback_data.get("before") if isinstance(rollback_data.get("before"), list) else []
    if not receipt_adopted or any(not isinstance(row, dict) for row in [*receipt_adopted, *originals]):
        raise CronManagerError("prepared adoption receipt is incomplete")
    original_adopted = validate_adoption_map(adoption_map, originals, desired, guild_id)
    receipt_pairs = {
        (str(row.get("role") or ""), str(row.get("jobId") or ""), str(row.get("jobSha256") or ""))
        for row in receipt_adopted
    }
    expected_pairs = {
        (role, str(job["id"]), job_fingerprint(job))
        for role, job in original_adopted.items()
    }
    if receipt_pairs != expected_pairs:
        raise CronManagerError("prepared adoption receipt does not match the authorized map")
    current_by_id = {str(job.get("id") or ""): job for job in inventory}
    current_adopted: dict[str, dict[str, Any]] = {}
    for role, original in original_adopted.items():
        current = current_by_id.get(str(original["id"]))
        if current is None:
            raise CronManagerError("prepared adopted job disappeared before apply")
        expected_disabled = {**original, "enabled": False}
        if job_contract(current) != job_contract(expected_disabled):
            raise CronManagerError("prepared adopted job changed after authorization")
        current_adopted[role] = current
    return current_adopted


def redacted_job(job: dict[str, Any]) -> dict[str, Any]:
    result = job_contract(job)
    result["id"] = job.get("id")
    payload = result.get("payload") or {}
    if "message" in payload:
        message = str(payload.pop("message"))
        payload["messageSha256"] = sha256_bytes(message.encode("utf-8"))
        payload["messageBytes"] = len(message.encode("utf-8"))
    return result


def write_receipt(receipt_dir: Path, *, plan: dict[str, Any], before: list[dict[str, Any]], desired: list[dict[str, Any]]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    secure_directory(receipt_dir)
    secure_directory(receipt_dir / "transactions")
    transaction = receipt_dir / "transactions" / stamp
    transaction.mkdir(mode=0o700, exist_ok=False)
    payload = {
        "schema": RECEIPT_SCHEMA,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "plan": plan,
        "before": [redacted_job(job) for job in before],
        "desired": [redacted_job(job) for job in desired],
    }
    encoded = canonical_json(payload)
    rollback_payload = {
        "schema": "openclaw-cron-rollback-contract.v1",
        "createdAt": payload["createdAt"],
        "before": [{"id": job.get("id"), **job_contract(job)} for job in before],
    }
    rollback_encoded = canonical_json(rollback_payload)
    atomic_write(transaction / "receipt.json", encoded)
    atomic_write(transaction / "receipt.sha256", (sha256_bytes(encoded) + "  receipt.json\n").encode("ascii"))
    atomic_write(transaction / "rollback.json", rollback_encoded)
    atomic_write(transaction / "rollback.sha256", (sha256_bytes(rollback_encoded) + "  rollback.json\n").encode("ascii"))
    return transaction


def secure_read_receipt_file(path: Path, max_bytes: int) -> bytes:
    reject_symlink_components(path, label="transaction receipt")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > max_bytes
        ):
            raise CronManagerError("transaction receipt file is unsafe")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_nlink)
        if len(data) > max_bytes or identity(before) != identity(after):
            raise CronManagerError("transaction receipt changed while being read")
        return data
    finally:
        os.close(descriptor)


def load_verified_receipt(transaction: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    transaction = Path(os.path.abspath(transaction))
    reject_symlink_components(transaction, label="transaction directory")
    info = transaction.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise CronManagerError("transaction directory is unsafe")
    loaded: dict[str, dict[str, Any]] = {}
    for name in ("receipt", "rollback"):
        payload_bytes = secure_read_receipt_file(transaction / f"{name}.json", MAX_RECEIPT_BYTES)
        digest_bytes = secure_read_receipt_file(transaction / f"{name}.sha256", MAX_DIGEST_BYTES)
        try:
            digest_text = digest_bytes.decode("ascii")
            expected, declared_name = digest_text.strip().split(None, 1)
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise CronManagerError("transaction receipt is malformed") from exc
        if declared_name.strip() != f"{name}.json" or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise CronManagerError("transaction receipt digest contract is malformed")
        if expected != sha256_bytes(payload_bytes) or not isinstance(payload, dict):
            raise CronManagerError("transaction receipt checksum verification failed")
        loaded[name] = payload
    return loaded["receipt"], loaded["rollback"]


def verify_receipt(transaction: Path) -> bool:
    try:
        load_verified_receipt(transaction)
        return True
    except (OSError, CronManagerError):
        return False


def rollback_committed_transaction(client: Any, transaction: Path) -> dict[str, Any]:
    if not verify_receipt(transaction):
        raise CronManagerError("committed transaction receipt failed checksum verification")
    receipt, rollback_data = load_verified_receipt(transaction)
    if receipt.get("schema") != RECEIPT_SCHEMA or rollback_data.get("schema") != "openclaw-cron-rollback-contract.v1":
        raise CronManagerError("unsupported rollback receipt schema")
    plan = receipt.get("plan") if isinstance(receipt.get("plan"), dict) else {}
    actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    before = rollback_data.get("before") if isinstance(rollback_data.get("before"), list) else []
    if any(not isinstance(row, dict) for row in [*actions, *before]):
        raise CronManagerError("rollback receipt content is malformed")
    action_keys: set[str] = set()
    for action in actions:
        key = str(action.get("declarationKey") or "")
        kind = action.get("action")
        if not key or kind not in {"create", "update", "unchanged"} or key in action_keys:
            raise CronManagerError("rollback receipt contains an invalid or duplicate action")
        action_keys.add(key)
    before_by_key = {str(row.get("declarationKey")): row for row in before if row.get("declarationKey")}
    before_by_id = {str(row.get("id")): row for row in before if row.get("id")}
    if len(before_by_key) != len([row for row in before if row.get("declarationKey")]):
        raise CronManagerError("rollback receipt contains duplicate declaration keys")
    if len(before_by_id) != len([row for row in before if row.get("id")]):
        raise CronManagerError("rollback receipt contains duplicate job IDs")
    current = client.list_jobs()
    current_by_key: dict[str, list[dict[str, Any]]] = {}
    for row in current:
        if row.get("declarationKey"):
            current_by_key.setdefault(str(row["declarationKey"]), []).append(row)

    adopted = plan.get("adopted") if isinstance(plan.get("adopted"), list) else []
    if any(not isinstance(row, dict) for row in adopted):
        raise CronManagerError("rollback adoption receipt is malformed")
    adopted_ids = {str(row.get("jobId") or "") for row in adopted}
    if "" in adopted_ids or len(adopted_ids) != len(adopted):
        raise CronManagerError("rollback adoption receipt contains invalid or duplicate job IDs")
    created_keys = {
        str(action.get("declarationKey") or "")
        for action in actions if action.get("action") == "create"
    }
    managed_keys = {
        str(action.get("declarationKey") or "")
        for action in actions if action.get("declarationKey")
    } | set(before_by_key)
    unknown_before = rollback_unknown_originals(
        current, managed_keys=managed_keys, managed_ids=adopted_ids,
    )
    owned_originals = [row for row in before if str(row.get("id") or "") not in adopted_ids]
    adopted_originals = [before_by_id[job_id] for job_id in adopted_ids if job_id in before_by_id]
    if len(adopted_originals) != len(adopted_ids):
        raise CronManagerError("rollback receipt is missing an adopted original")

    restored = 0
    removed = 0
    operation_errors: list[str] = []
    for action in actions:
        key = str(action.get("declarationKey") or "")
        kind = action.get("action")
        if kind == "create":
            matches = current_by_key.get(key, [])
            if len(matches) > 1:
                raise CronManagerError("cannot rollback duplicate created declaration")
            if matches:
                try:
                    client.remove(str(matches[0]["id"]))
                    removed += 1
                except Exception as exc:
                    operation_errors.append(f"remove-created:{key}:{type(exc).__name__}")
        elif kind == "update":
            original = before_by_key.get(key)
            if original is None:
                raise CronManagerError("rollback receipt is missing an updated original")
            try:
                client.restore(original)
                restored += 1
            except Exception as exc:
                operation_errors.append(f"restore-owned:{key}:{type(exc).__name__}")

    for row in adopted:
        original = before_by_id.get(str(row.get("jobId") or ""))
        if original is None:
            raise CronManagerError("rollback receipt is missing an adopted original")
        try:
            client.set_enabled(str(original["id"]), bool(original.get("enabled")))
            restored += 1
        except Exception as exc:
            operation_errors.append(f"restore-adopted:{original.get('id')}:{type(exc).__name__}")

    try:
        final = client.list_jobs()
        verification_errors = rollback_verification_errors(
            final,
            owned_originals=owned_originals,
            adopted_originals=adopted_originals,
            created_keys=created_keys,
            unknown_originals=unknown_before,
        )
    except Exception as exc:
        operation_errors.append(f"verify-inventory:{type(exc).__name__}")
        verification_errors = []
    all_errors = [*operation_errors, *verification_errors]
    if all_errors:
        raise CronRollbackIncompleteError(
            f"committed rollback was incomplete: {len(all_errors)} operation or verification check(s) failed"
        )
    result = {"status": "rolled_back_after_commit", "removed": removed, "restored": restored}
    atomic_write(transaction / "post-install-rollback.json", canonical_json(result))
    return result


def extract_job(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        if isinstance(payload.get("job"), dict) and payload["job"].get("id"):
            return payload["job"]
        if payload.get("id"):
            return payload
        for value in payload.values():
            try:
                return extract_job(value)
            except CronManagerError:
                continue
    raise CronManagerError("OpenClaw cron response did not contain a job")


class OpenClawCronClient:
    def __init__(self, binary: str = "openclaw", timeout_seconds: int = 120) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds

    def run(self, args: list[str], *, timeout_seconds: int | None = None) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                [self.binary, *args], text=True, capture_output=True,
                timeout=timeout_seconds or self.timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CronManagerError(f"OpenClaw cron command failed to start or timed out: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            # CLI output is untrusted and may contain tokens, message bodies, or
            # customer paths. Never echo it into terminal-visible diagnostics or
            # transaction receipts.
            raise CronManagerError(f"OpenClaw cron command failed (exit={proc.returncode}, category=command_error)")
        return proc

    def list_jobs(self) -> list[dict[str, Any]]:
        proc = self.run(["cron", "list", "--all", "--json"])
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise CronManagerError("OpenClaw cron list returned invalid JSON") from exc
        return validate_inventory(payload)

    def converge_disabled(self, desired: dict[str, Any]) -> dict[str, Any]:
        proc = self.run(cron_add_args(desired, disabled=True))
        try:
            job = extract_job(json.loads(proc.stdout))
        except json.JSONDecodeError as exc:
            raise CronManagerError("OpenClaw cron add returned invalid JSON") from exc
        self.configure_post_add(str(job["id"]), desired)
        return job

    def configure_post_add(self, job_id: str, desired: dict[str, Any], *, preserve_tools: bool = False) -> None:
        alert = desired.get("failureAlert")
        args = ["cron", "edit", job_id]
        tools = (desired.get("payload") or {}).get("toolsAllow") if isinstance(desired.get("payload"), dict) else None
        if preserve_tools and tools is not None:
            args.extend(["--tools", ",".join(str(item) for item in tools)])
        else:
            args.append("--clear-tools")
        if alert:
            args.extend([
                "--failure-alert",
                "--failure-alert-after", str(alert["after"]),
                "--failure-alert-channel", str(alert["channel"]),
                "--failure-alert-to", str(alert["to"]),
                "--failure-alert-cooldown", "1h",
                "--failure-alert-exclude-skipped",
                "--failure-alert-mode", str(alert["mode"]),
            ])
            if alert.get("accountId"):
                args.extend(["--failure-alert-account-id", str(alert["accountId"])])
        else:
            args.append("--no-failure-alert")
        args.append("--disable")
        self.run(args)

    def set_enabled(self, job_id: str, enabled: bool) -> None:
        self.run(["cron", "edit", job_id, "--enable" if enabled else "--disable"])

    def restore(self, original: dict[str, Any]) -> dict[str, Any]:
        proc = self.run(cron_add_args(original, disabled=True))
        try:
            job = extract_job(json.loads(proc.stdout))
        except json.JSONDecodeError as exc:
            raise CronManagerError("OpenClaw cron restore returned invalid JSON") from exc
        self.configure_post_add(str(job["id"]), original, preserve_tools=True)
        self.set_enabled(str(job["id"]), bool(original.get("enabled")))
        return job

    def remove(self, job_id: str) -> None:
        self.run(["cron", "rm", job_id, "--json"])

    def remove_declaration_key(self, declaration_key: str) -> None:
        matches = [job for job in self.list_jobs() if str(job.get("declarationKey") or "") == declaration_key]
        for job in matches:
            self.remove(str(job["id"]))
        remaining = [job for job in self.list_jobs() if str(job.get("declarationKey") or "") == declaration_key]
        if remaining:
            raise CronManagerError("temporary canary declaration remained after cleanup")

    def canary(self, workspace: Path) -> None:
        key = f"openclaw-discord-server-backup-canary-{os.getpid()}-{int(time.time()*1000)}"
        args = [
            "cron", "add", "--name", "Discord backup isolated command canary",
            "--at", "+1h", "--declaration-key", key,
            "--session", "isolated", "--exact", "--no-deliver",
            "--command-argv", json.dumps([sys.executable, "-c", "from pathlib import Path; print(Path.cwd()); print('TOOL_OK')"]),
            "--command-cwd", str(workspace), "--timeout-seconds", "60",
            "--no-output-timeout-seconds", "30", "--output-max-bytes", "8192", "--json",
        ]
        job_id: str | None = None
        primary_error: Exception | None = None
        cleanup_error: Exception | None = None
        try:
            job = extract_job(json.loads(self.run(args).stdout))
            job_id = str(job["id"])
            self.run(["cron", "run", job_id, "--wait", "--wait-timeout", "2m"], timeout_seconds=180)
            history = self.run(["cron", "runs", "--id", job_id, "--limit", "1"])
            payload = json.loads(history.stdout)
            entries = payload.get("entries") if isinstance(payload, dict) else None
            newest = entries[0] if isinstance(entries, list) and entries else {}
            if newest.get("status") != "ok" or "TOOL_OK" not in str(newest.get("summary") or ""):
                raise CronManagerError("isolated command canary did not produce TOOL_OK")
        except Exception as exc:
            primary_error = exc
        finally:
            try:
                self.remove_declaration_key(key)
            except Exception as exc:
                cleanup_error = exc
        if cleanup_error:
            raise CronManagerError("isolated command canary cleanup failed") from cleanup_error
        if primary_error:
            raise primary_error

    def persistent_session_canary(self, workspace: Path, session_target: str, agent: str) -> None:
        if not session_target.startswith("session:"):
            raise CronManagerError("persistent-session canary requires a session: target")
        helper = Path(__file__).resolve().parent / "daily_sync_overlap_canary.py"
        if helper.is_symlink() or not helper.is_file():
            raise CronManagerError("persistent-session canary helper is missing or unsafe")
        job_ids: list[str] = []
        declaration_keys: list[str] = []
        cleanup_errors: list[str] = []
        primary_error: Exception | None = None
        processes: list[subprocess.Popen[str]] = []
        with tempfile.TemporaryDirectory(prefix="openclaw-daily-session-canary-") as tmp:
            state_dir = Path(tmp) / "state"
            try:
                for label in ("A", "B"):
                    key = f"openclaw-discord-session-canary-{os.getpid()}-{int(time.time()*1000)}-{label.lower()}"
                    declaration_keys.append(key)
                    helper_argv = json.dumps([
                        sys.executable, str(helper), "--state-dir", str(state_dir), "--label", label,
                    ])
                    message = (
                        "Use the exec tool exactly once with this argv JSON, wait for completion, "
                        f"and return its stdout only: {helper_argv}"
                    )
                    args = [
                        "cron", "add", "--name", f"Discord daily session canary {label}",
                        "--at", "+1h", "--declaration-key", key,
                        "--session", session_target, "--exact", "--no-deliver",
                        "--message", message, "--agent", agent, "--tools", "exec",
                        "--light-context", "--timeout-seconds", "120", "--json",
                    ]
                    job_ids.append(str(extract_job(json.loads(self.run(args).stdout))["id"]))
                processes = [
                    subprocess.Popen(
                        [self.binary, "cron", "run", job_id, "--wait", "--wait-timeout", "2m"],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    for job_id in job_ids
                ]
                for process in processes:
                    _, _stderr = process.communicate(timeout=180)
                    if process.returncode != 0:
                        raise CronManagerError(
                            f"persistent-session overlap canary failed (exit={process.returncode})"
                        )
                trace = state_dir / "trace.jsonl"
                if (state_dir / "overlap").exists() or not trace.is_file():
                    raise CronManagerError("persistent-session overlap canary detected concurrent execution")
                events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
                if [event.get("event") for event in events] not in (["start", "end", "start", "end"],):
                    raise CronManagerError("persistent-session overlap canary did not prove serialized ordering")
            except Exception as exc:
                primary_error = exc
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        try:
                            process.communicate(timeout=10)
                        except subprocess.TimeoutExpired:
                            cleanup_errors.append("process")
                for key in reversed(declaration_keys):
                    try:
                        self.remove_declaration_key(key)
                    except Exception:
                        cleanup_errors.append(key)
        if cleanup_errors:
            raise CronManagerError(f"persistent-session canary cleanup failed for {len(cleanup_errors)} job(s)")
        if primary_error:
            raise primary_error


def cron_add_args(job: dict[str, Any], *, disabled: bool) -> list[str]:
    schedule = job["schedule"]
    payload = job["payload"]
    args = [
        "cron", "add", "--name", str(job["name"]),
        "--description", str(job["description"]),
        "--cron", str(schedule["expr"]), "--tz", str(schedule["tz"]), "--exact",
        "--declaration-key", str(job["declarationKey"]),
        "--session", str(job["sessionTarget"]),
    ]
    if payload["kind"] == "command":
        args.extend([
            "--command-argv", json.dumps(payload["argv"], ensure_ascii=False),
            "--command-cwd", str(payload["cwd"]),
            "--timeout-seconds", str(payload["timeoutSeconds"]),
            "--no-output-timeout-seconds", str(payload["noOutputTimeoutSeconds"]),
            "--output-max-bytes", str(payload["outputMaxBytes"]),
        ])
    elif payload["kind"] == "agentTurn":
        args.extend([
            "--message", str(payload["message"]),
            "--agent", str(job["agentId"]),
            "--timeout-seconds", str(payload["timeoutSeconds"]),
            "--light-context",
        ])
    else:
        raise CronManagerError("unsupported desired payload kind")
    delivery = normalized_delivery(job.get("delivery"))
    if delivery["mode"] == "none":
        args.append("--no-deliver")
    else:
        args.extend(["--announce", "--channel", str(delivery["channel"]), "--to", str(delivery["to"])])
        if delivery.get("accountId"):
            args.extend(["--account", str(delivery["accountId"])])
    if disabled:
        args.append("--disabled")
    args.append("--json")
    return args


def rollback(
    client: Any,
    before_by_key: dict[str, dict[str, Any]],
    created_keys: list[str],
    changed_keys: list[str],
    adopted_originals: list[dict[str, Any]] | None = None,
    disabled_owned: list[dict[str, Any]] | None = None,
    inventory_before: list[dict[str, Any]] | None = None,
    all_adopted_originals: list[dict[str, Any]] | None = None,
) -> list[str]:
    errors: list[str] = []
    # Keys are registered before `cron add` is called. This lets rollback find
    # and remove a newly created job even when add mutated durable state but its
    # JSON readback or subsequent alert attachment failed.
    for key in reversed(created_keys):
        try:
            matches = [job for job in client.list_jobs() if str(job.get("declarationKey") or "") == key]
            if len(matches) > 1:
                raise CronManagerError("created declaration is duplicated during rollback")
            if matches:
                client.remove(str(matches[0]["id"]))
        except Exception as exc:
            errors.append(f"remove-created:{key}:{type(exc).__name__}")
    for key in reversed(changed_keys):
        original = before_by_key.get(key)
        if not original:
            continue
        try:
            client.restore(original)
        except Exception as exc:
            errors.append(f"restore-owned:{key}:{type(exc).__name__}")
    for original in reversed(adopted_originals or []):
        try:
            client.set_enabled(str(original["id"]), bool(original.get("enabled")))
        except Exception as exc:
            errors.append(f"restore-adopted:{original.get('id')}:{type(exc).__name__}")
    for original in reversed(disabled_owned or []):
        try:
            client.set_enabled(str(original["id"]), bool(original.get("enabled")))
        except Exception as exc:
            errors.append(f"restore-enabled:{original.get('id')}:{type(exc).__name__}")
    adopted_rows = list(all_adopted_originals if all_adopted_originals is not None else (adopted_originals or []))
    original_owned = list(before_by_key.values())
    managed_ids = {str(row.get("id") or "") for row in adopted_rows}
    managed_keys = set(before_by_key) | {str(key) for key in created_keys}
    unknown_before = rollback_unknown_originals(
        inventory_before or [], managed_keys=managed_keys, managed_ids=managed_ids,
    )
    try:
        errors.extend(rollback_verification_errors(
            client.list_jobs(),
            owned_originals=original_owned,
            adopted_originals=adopted_rows,
            created_keys=created_keys,
            unknown_originals=unknown_before,
        ))
    except Exception as exc:
        errors.append(f"verify-inventory:{type(exc).__name__}")
    return errors


def best_effort_result(transaction: Path, payload: dict[str, Any]) -> bool:
    try:
        atomic_write(transaction / "result.json", canonical_json(payload))
        return True
    except Exception:
        return False


def job_appears_running(job: dict[str, Any]) -> bool:
    state = job.get("state") if isinstance(job.get("state"), dict) else {}
    return bool(
        job.get("running") is True
        or state.get("running") is True
        or state.get("runningAtMs")
        or state.get("startedAtMs")
    )


def prepare_quiescence(
    client: Any,
    inventory: list[dict[str, Any]],
    desired: list[dict[str, Any]],
    adopted: dict[str, dict[str, Any]],
    receipt_dir: Path,
) -> dict[str, Any]:
    validate_restorable_owned_jobs(inventory, desired)
    adopted_ids = {str(job["id"]) for job in adopted.values()}
    topology_plan = build_plan(inventory, desired, adopted_job_ids=adopted_ids)
    if not topology_plan["ok"]:
        raise CronManagerError("cron topology is ambiguous; quiescence was not started")
    owned_keys = {str(job["declarationKey"]) for job in desired}
    owned = [job for job in inventory if str(job.get("declarationKey") or "") in owned_keys]
    rows_by_id = {str(job["id"]): job for job in [*owned, *adopted.values()]}
    rows = list(rows_by_id.values())
    if any(job_appears_running(job) for job in rows):
        raise CronManagerError("an owned or adopted backup job is currently running; retry after it becomes quiescent")
    if not rows:
        return {"status": "no_quiescence_needed", "transaction": None, "jobs": 0}
    role_by_key = {str(job["declarationKey"]): str(job["role"]) for job in desired}
    plan = {
        "ok": True,
        "actions": [
            {
                "role": role_by_key[str(job["declarationKey"])],
                "declarationKey": str(job["declarationKey"]),
                "action": "update",
                "jobId": str(job["id"]),
            }
            for job in owned
        ],
        "duplicateOwnedKeys": [],
        "unknownCollisions": [],
        "summary": {"create": 0, "update": len(owned), "unchanged": 0},
        "adopted": [
            {"role": role, "jobId": str(job["id"]), "jobSha256": job_fingerprint(job)}
            for role, job in sorted(adopted.items())
        ],
    }
    transaction = write_receipt(receipt_dir, plan=plan, before=rows, desired=[])
    if not verify_receipt(transaction):
        raise CronManagerError("quiescence prepare receipt verification failed")
    disabled: list[dict[str, Any]] = []
    try:
        for original in rows:
            if bool(original.get("enabled")):
                client.set_enabled(str(original["id"]), False)
                disabled.append(original)
        by_id = {str(job.get("id") or ""): job for job in client.list_jobs()}
        if any(by_id.get(str(job["id"]), {}).get("enabled") is not False for job in rows):
            raise CronManagerError("an owned or adopted job remained enabled during quiescence prepare")
        if any(job_appears_running(job) for job in by_id.values() if str(job.get("id") or "") in rows_by_id):
            raise CronManagerError("an owned or adopted backup job remained running after disable")
    except Exception as exc:
        errors: list[str] = []
        for original in reversed(disabled):
            try:
                client.set_enabled(str(original["id"]), bool(original.get("enabled")))
            except Exception as restore_exc:
                errors.append(f"restore-adopted:{original.get('id')}:{type(restore_exc).__name__}")
        unknown_before = rollback_unknown_originals(
            inventory,
            managed_keys={str(job.get("declarationKey") or "") for job in owned},
            managed_ids={str(job.get("id") or "") for job in adopted.values()},
        )
        try:
            errors.extend(rollback_verification_errors(
                client.list_jobs(),
                owned_originals=owned,
                adopted_originals=adopted.values(),
                created_keys=[],
                unknown_originals=unknown_before,
            ))
        except Exception as verify_exc:
            errors.append(f"verify-inventory:{type(verify_exc).__name__}")
        best_effort_result(transaction, {
            "status": "rollback_incomplete" if errors else "rolled_back",
            "error": type(exc).__name__,
            "rollbackErrors": errors,
        })
        if errors:
            raise CronRollbackIncompleteError("quiescence prepare failed and rollback was incomplete") from exc
        raise
    result = {"status": "quiescence_prepared", "transaction": str(transaction), "jobs": len(rows)}
    # The checksummed receipt/rollback contract was committed before mutation
    # and is the transaction authority. A cosmetic result-file failure after
    # successful disable must not hide the transaction path from the caller or
    # trigger a mixed file/runtime rollback.
    result["resultRecorded"] = best_effort_result(transaction, result)
    return result


def apply_plan(
    client: Any,
    inventory: list[dict[str, Any]],
    desired: list[dict[str, Any]],
    receipt_dir: Path,
    workspace: Path,
    *,
    run_canary: bool = True,
    fault_after: int | None = None,
    adopted: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    adopted = adopted or {}
    validate_restorable_owned_jobs(inventory, desired)
    adopted_ids = {str(job["id"]) for job in adopted.values()}
    plan = build_plan(inventory, desired, adopted_job_ids=adopted_ids)
    if not plan["ok"]:
        raise CronManagerError("cron topology is ambiguous; unknown jobs were preserved and no changes were made")
    if (
        plan["summary"] == {"create": 0, "update": 0, "unchanged": len(desired)}
        and all(job.get("enabled") is False for job in adopted.values())
    ):
        guild_id = str(desired[0]["declarationKey"]).split(":", 2)[1]
        write_topology_component(receipt_dir, guild_id, desired)
        return {"status": "ready", "transaction": None, "mutations": 0, "jobs": len(desired), "summary": plan["summary"]}
    active_candidates = [
        job for job in inventory
        if str(job.get("declarationKey") or "") in {str(item["declarationKey"]) for item in desired}
    ] + list(adopted.values())
    if any(job_appears_running(job) for job in active_candidates):
        raise CronManagerError("an owned or adopted backup job is currently running; retry after it becomes quiescent")
    before_by_key = {
        str(job["declarationKey"]): job
        for job in inventory
        if job.get("declarationKey") in {item["declarationKey"] for item in desired}
    }
    transaction = write_receipt(
        receipt_dir, plan={**plan, "adopted": [{"role": role, "jobId": str(job["id"]), "jobSha256": job_fingerprint(job)} for role, job in sorted(adopted.items())]},
        before=[*before_by_key.values(), *adopted.values()], desired=desired,
    )
    if not verify_receipt(transaction):
        raise CronManagerError("pre-change receipt verification failed")
    created_keys: list[str] = []
    changed_keys: list[str] = []
    staged_ids: list[str] = []
    disabled_adopted: list[dict[str, Any]] = []
    disabled_owned: list[dict[str, Any]] = []
    mutations = 0
    try:
        by_role = {job["role"]: job for job in desired}
        # A validated adopted legacy job must be quiet before any owned job is
        # staged or any canary runs. In normal installer flow this is already
        # done by the pre-swap adoption prepare transaction; direct manager use
        # receives the same protection here.
        for role, legacy in sorted(adopted.items()):
            expected = by_role[role]
            if any(job.get("declarationKey") == expected["declarationKey"] for job in inventory):
                raise CronManagerError(f"cannot adopt {role}: owned declaration already exists")
            if bool(legacy.get("enabled")):
                client.set_enabled(str(legacy["id"]), False)
                disabled_adopted.append(legacy)
                mutations += 1
                if fault_after is not None and mutations >= fault_after:
                    raise CronManagerError("injected reconciliation failure")
        for original in before_by_key.values():
            if bool(original.get("enabled")):
                client.set_enabled(str(original["id"]), False)
                disabled_owned.append(original)
                mutations += 1
                if fault_after is not None and mutations >= fault_after:
                    raise CronManagerError("injected reconciliation failure")
        for action in plan["actions"]:
            if action["action"] == "unchanged":
                continue
            expected = by_role[action["role"]]
            if action["action"] == "create":
                created_keys.append(expected["declarationKey"])
            else:
                changed_keys.append(expected["declarationKey"])
            result = client.converge_disabled(expected)
            staged_ids.append(str(result["id"]))
            mutations += 1
            if fault_after is not None and mutations >= fault_after:
                raise CronManagerError("injected reconciliation failure")
        staged_inventory = client.list_jobs()
        staged_by_key = {str(job.get("declarationKey") or ""): job for job in staged_inventory}
        changed_key_set = {by_role[row["role"]]["declarationKey"] for row in plan["actions"] if row["action"] != "unchanged"}
        for expected in desired:
            actual = staged_by_key.get(expected["declarationKey"])
            if actual is None:
                raise CronManagerError("staged topology is missing an owned job")
            expected_disabled = {**expected, "enabled": False}
            if job_contract(actual) != job_contract(expected_disabled):
                raise CronManagerError(f"staged topology verification failed for {expected['role']}")
        if run_canary:
            client.canary(workspace)
            daily_target = next(job["sessionTarget"] for job in desired if job["role"] == "daily-sync-1")
            if hasattr(client, "persistent_session_canary"):
                client.persistent_session_canary(workspace, daily_target, str(by_role["daily-sync-1"]["agentId"]))
        staged_inventory = client.list_jobs()
        staged_by_key = {str(job.get("declarationKey") or ""): job for job in staged_inventory}
        for expected in desired:
            actual = staged_by_key.get(expected["declarationKey"])
            if actual is None:
                raise CronManagerError("owned job disappeared before activation")
            client.set_enabled(str(actual["id"]), True)
            mutations += 1
            if fault_after is not None and mutations >= fault_after:
                raise CronManagerError("injected reconciliation failure")
        verified_inventory = client.list_jobs()
        verified_by_id = {str(job.get("id") or ""): job for job in verified_inventory}
        if any(verified_by_id.get(str(job["id"]), {}).get("enabled") is not False for job in disabled_adopted):
            raise CronManagerError("an adopted legacy job remained enabled")
        verification = build_plan(verified_inventory, desired, adopted_job_ids=adopted_ids)
        if not verification["ok"] or verification["summary"] != {"create": 0, "update": 0, "unchanged": len(desired)}:
            raise CronManagerError("post-change topology verification failed")
        guild_id = str(desired[0]["declarationKey"]).split(":", 2)[1]
        write_topology_component(receipt_dir, guild_id, desired)
    except Exception as exc:
        rollback_errors = rollback(
            client,
            before_by_key,
            created_keys,
            changed_keys,
            disabled_adopted,
            disabled_owned,
            inventory,
            list(adopted.values()),
        )
        best_effort_result(transaction, {
            "status": "rolled_back" if not rollback_errors else "rollback_incomplete",
            "error": type(exc).__name__,
            "rollbackErrors": rollback_errors,
        })
        if rollback_errors:
            raise CronRollbackIncompleteError("reconciliation failed and rollback was incomplete") from exc
        raise
    result = {
        "status": "ready",
        "transaction": str(transaction),
        "mutations": mutations,
        "jobs": len(desired),
        "summary": plan["summary"],
    }
    # All jobs are already verified and the pre-change rollback receipt is
    # durable. Keep success observable even if this optional summary file can no
    # longer be written (for example, a full disk after activation).
    result["resultRecorded"] = best_effort_result(transaction, result)
    return result


def compact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": plan["ok"],
        "summary": plan["summary"],
        "duplicateOwnedKeys": plan["duplicateOwnedKeys"],
        "unknownCollisions": plan["unknownCollisions"],
        "actions": plan["actions"],
    }


def write_topology_component(receipt_dir: Path, guild_id: str, desired: list[dict[str, Any]]) -> None:
    payload = {
        "schema": "backup-health-component.v1",
        "producer": "openclaw-discord-server-backup/cron-manager.v1",
        "declarationKey": f"openclaw-discord-server-backup:{guild_id}:topology:v1",
        "component": "cron-topology",
        "status": "ok",
        "checkedAt": datetime.now().astimezone().isoformat(),
        "summary": "排程、序列化與失敗告警驗證通過",
        "checks": [
            {"key": "owned_jobs", "status": "ok", "summary": f"{len(desired)} 個 owned jobs 已驗證"},
            {"key": "failure_alerts", "status": "ok", "summary": "每項任務皆在一次錯誤後告警，安全略過不計錯誤"},
            {"key": "daily_session", "status": "ok", "summary": "三段日常同步使用同一持久 session"},
        ],
        "metrics": {"ownedJobs": len(desired)},
        "anomalies": [],
        "pending": [],
    }
    atomic_write(receipt_dir / "components" / "cron-topology.json", (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the owned OpenClaw Discord backup cron topology.")
    parser.add_argument("operation", choices=("plan", "apply", "verify", "prepare-quiescence", "verify-receipt", "rollback-receipt", "validate-manifest", "fingerprint-job"))
    parser.add_argument("--manifest", default=str(Path(__file__).resolve().parents[1] / "manifests/owned-cron.v1.json"))
    parser.add_argument("--workspace")
    parser.add_argument("--skill-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--config")
    parser.add_argument("--backup-root")
    parser.add_argument("--guild-id")
    parser.add_argument("--report-to")
    parser.add_argument("--agent", default="main")
    parser.add_argument("--timezone", default="Asia/Taipei")
    parser.add_argument("--receipt-dir")
    parser.add_argument("--account-id")
    parser.add_argument("--python-executable", default="python3")
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--skip-canary", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fault-after", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--receipt", help="Transaction directory for verify-receipt")
    parser.add_argument("--adoption-map", help="Explicit checksummed legacy-job adoption map")
    parser.add_argument("--prepared-adoption-receipt", help="Verified pre-swap adoption prepare transaction")
    parser.add_argument("--job-id", help="Exact job ID for fingerprint-job")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.operation == "validate-manifest":
        try:
            manifest = read_json(Path(args.manifest))
            validate_manifest(manifest)
            print(json.dumps({"ok": True, "jobs": len(manifest["jobs"]), "schema": manifest["schema"]}))
            return 0
        except CronManagerError as exc:
            print(json.dumps({"status": "BLOCKED", "error": str(exc)}), file=sys.stderr)
            return 2
    if args.operation == "verify-receipt":
        if not args.receipt:
            raise SystemExit("--receipt is required")
        ok = verify_receipt(Path(args.receipt))
        print(json.dumps({"ok": ok, "receipt": str(Path(args.receipt))}))
        return 0 if ok else 2
    if args.operation == "rollback-receipt":
        if not args.receipt:
            raise SystemExit("--receipt is required")
        try:
            result = rollback_committed_transaction(OpenClawCronClient(args.openclaw_bin), Path(args.receipt))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except CronManagerError as exc:
            print(json.dumps({"status": "BLOCKED", "error": str(exc)}), file=sys.stderr)
            return 2
    if args.operation == "fingerprint-job":
        if not args.job_id:
            raise SystemExit("--job-id is required")
        client = OpenClawCronClient(args.openclaw_bin)
        matches = [job for job in client.list_jobs() if str(job.get("id") or "") == args.job_id]
        if len(matches) != 1:
            print(json.dumps({"status": "BLOCKED", "error": "exact job ID not found"}), file=sys.stderr)
            return 2
        print(json.dumps({"jobId": args.job_id, "jobSha256": job_fingerprint(matches[0])}, ensure_ascii=False))
        return 0
    required = ("workspace", "config", "backup_root", "guild_id", "report_to", "receipt_dir")
    missing = [name for name in required if not getattr(args, name)]
    if missing:
        raise SystemExit("missing required options: " + ", ".join("--" + name.replace("_", "-") for name in missing))
    try:
        manifest = read_json(Path(args.manifest))
        bound_python = trusted_executable(args.python_executable, label="Python")
        bound_openclaw = trusted_executable(args.openclaw_bin, label="OpenClaw")
        context = RenderContext(
            workspace=Path(os.path.abspath(Path(args.workspace).expanduser())),
            skill_dir=Path(os.path.abspath(Path(args.skill_dir).expanduser())),
            config_path=Path(os.path.abspath(Path(args.config).expanduser())),
            backup_root=Path(os.path.abspath(Path(args.backup_root).expanduser())),
            guild_id=args.guild_id,
            report_to=args.report_to,
            agent=args.agent,
            timezone_name=args.timezone,
            receipt_dir=Path(os.path.abspath(Path(args.receipt_dir).expanduser())),
            account_id=args.account_id,
            python_executable=bound_python,
            openclaw_binary=bound_openclaw,
        )
        desired = render_jobs(manifest, context)
        client = OpenClawCronClient(bound_openclaw)
        inventory = client.list_jobs()
        adoption_path = Path(os.path.abspath(Path(args.adoption_map).expanduser())) if args.adoption_map else None
        if args.prepared_adoption_receipt:
            if adoption_path is None or args.operation not in {"apply", "verify", "plan", "prepare-quiescence"}:
                raise CronManagerError("prepared adoption receipt requires an adoption map during topology reconciliation")
            adopted = validate_prepared_adoption(
                Path(os.path.abspath(Path(args.prepared_adoption_receipt).expanduser())),
                adoption_path, inventory, desired, context.guild_id,
            )
        else:
            adopted = validate_adoption_map(adoption_path, inventory, desired, context.guild_id)
        adopted_ids = {str(job["id"]) for job in adopted.values()}
        plan = build_plan(inventory, desired, adopted_job_ids=adopted_ids)
        if adopted:
            plan["adopted"] = [{"role": role, "jobId": str(job["id"])} for role, job in sorted(adopted.items())]
        if args.operation == "plan":
            print(json.dumps(compact_plan(plan), ensure_ascii=False, indent=2))
            return 0 if plan["ok"] else 2
        if args.operation == "verify":
            ready = plan["ok"] and plan["summary"] == {"create": 0, "update": 0, "unchanged": len(desired)}
            if ready:
                by_id = {str(job.get("id") or ""): job for job in inventory}
                ready = all(by_id.get(str(job["id"]), {}).get("enabled") is False for job in adopted.values())
            if ready:
                write_topology_component(context.receipt_dir, context.guild_id, desired)
            print(json.dumps({"ok": ready, **compact_plan(plan)}, ensure_ascii=False, indent=2))
            return 0 if ready else 2
        if args.operation == "prepare-quiescence":
            result = prepare_quiescence(client, inventory, desired, adopted, context.receipt_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        result = apply_plan(
            client, inventory, desired, context.receipt_dir, context.workspace,
            run_canary=not args.skip_canary, fault_after=args.fault_after, adopted=adopted,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CronManagerError as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
