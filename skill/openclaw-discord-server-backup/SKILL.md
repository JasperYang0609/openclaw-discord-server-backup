---
name: openclaw-discord-server-backup
description: OpenClaw-specific Discord server, channel, and thread backup skill with V3 cursor state, backlog queue, deterministic workers, audit probes, recovery workflows, and customer-installable configuration. Use when setting up, running, auditing, migrating, packaging, or troubleshooting OpenClaw Discord backups that must not miss messages, especially high-volume channels, threads over daily limits, queue catch-up, or GitHub/customer deployment.
---

# OpenClaw Discord Server Backup

## Non-negotiable guarantee

Do not infer completion from dates. `lastBackup` is only a check/report date.

A channel/thread is caught up only when `read after=<lastWrittenMessageId>` returns 0 messages.

## Standard workflow

1. Discovery registers channels/threads and creates folders. It does not read message content.
2. Daily sync processes only healthy entries in small batches.
3. If daily sync hits a page/message limit, it writes what it has, advances cursor only to written raw data, marks the entry partial, and enqueues backlog.
4. Backlog worker processes queue-first using deterministic scripts.
5. Audit probes every registered entry and requeues false-healthy entries.

## Scripts

Use scripts for fragile operations. Do not manually invent state transitions.

- `scripts/install.py`: install/copy skill and create local config/state/queue scaffolding.
- `scripts/bootstrap_state.py`: create/update entries from discovery inventory.
- `scripts/migrate_state_v3.py`: upgrade existing state and build queue from partial entries.
- `scripts/select_backlog_candidates.py`: choose queue-first backlog candidates.
- `scripts/run_backlog_worker_v3.py`: deterministic Discord API backlog worker.
- `scripts/audit_caught_up_v3.py`: full live probe and optional requeue.
- `scripts/package_skill.py`: build `.skill` artifact.

## References

- Read `references/architecture.md` for system design.
- Read `references/state-schema.md` before editing state/queue format.
- Read `references/customer-install.md` for installation and cron setup.
- Read `references/recovery.md` for restore/migration.
- Read `references/troubleshooting.md` for known failure modes.
- Read `references/llm-handoff.md` when another model needs to operate this skill.

## LLM compatibility rule

This skill must be understandable by Claude Opus, Sonnet, GPT, Gemini, and future OpenClaw-supported models.

Use plain explicit instructions, fixed status/reason enums, exact commands, and deterministic scripts. The LLM should run commands and summarize results, not reason out backup state transitions from scratch.
