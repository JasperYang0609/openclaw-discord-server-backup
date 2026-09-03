---
name: openclaw-discord-server-backup
description: OpenClaw-specific Discord server, channel, and thread backup skill with V3 cursor state, backlog queue, deterministic workers, audit probes, optional LanceDB knowledge indexing, recovery workflows, and customer-installable configuration. Use when setting up, running, auditing, migrating, packaging, or troubleshooting OpenClaw Discord backups that must not miss messages, especially high-volume channels, threads over daily limits, queue catch-up, or GitHub/customer deployment.
---

# OpenClaw Discord Server Backup

## Layering

This repo ships the merged customer version of a two-layer design:

- Engine layer (single source of truth): the deterministic scripts under `scripts/`
  (`run_backlog_worker_v3.py`, `migrate_state_v3.py`, `select_backlog_candidates.py`,
  `audit_caught_up_v3.py`, `bootstrap_state.py`, `core_workspace_backup.py`) plus the prompt templates under
  `prompts/`. All state/queue transitions are defined here and only here.
- Install layer: `scripts/install.py`, `scripts/init_config.py`, `examples/`, and any
  rendered cron prompts. Install materials must be derived from the engine prompts and
  scripts. Never hand-copy or fork engine logic into install materials; when the engine
  changes, regenerate the install side from it.

## Non-negotiable guarantee

Do not infer completion from dates. `lastBackup` is only a check/report date.

A channel/thread is caught up only when `read after=<lastWrittenMessageId>` returns 0 messages.

## Standard workflow

0. Core workspace backup runs `scripts/core_workspace_backup.py`: it stages and manifest-verifies `核心文件/latest/`, preserves one immutable daily snapshot, and supports an isolated restore canary for root-level Markdown files plus `memory/`. Use `prompts/core-backup.md`; never hardcode customer filenames or reimplement copy logic in a prompt.
1. Discovery registers channels/threads and creates folders. It does not read message content.
2. Daily sync processes only healthy entries in small batches.
3. If daily sync hits a page/message limit, it writes what it has, advances cursor only to written raw data, marks the entry partial, and enqueues backlog.
4. Backlog worker processes queue-first using deterministic scripts and emits `auditWarnings` for stuck active catch-ups (`attempts > 5` on active queue items, `consecutiveErrors > 3` on entries). Schedule routine runs only at `10 0,1,2,3,4,23 * * *` in the customer timezone: 23:10 and 00:10–04:10. Do not add daytime runs; carry unfinished queue debt to the next night without increasing the bounded worker limits.
5. Audit probes every registered entry and requeues false-healthy entries.
6. Optional LanceDB indexing runs after backup so summaries/raw outputs become searchable knowledge.

## Scripts

Use scripts for fragile operations. Do not manually invent state transitions.

- `scripts/core_workspace_backup.py`: deterministic core backup, exact manifest verification, and temporary restore canary.
- `scripts/install.py`: install/copy the skill; for fresh macOS installs, require the
  Discord server display name and create the real Desktop root
  `<伺服器名稱>資料備份/Discord資料`, with matching absolute config/state paths.
- `scripts/bootstrap_state.py`: create/update entries from discovery inventory.
- `scripts/migrate_state_v3.py`: upgrade existing state and build queue from partial entries.
- `scripts/select_backlog_candidates.py`: choose queue-first backlog candidates.
- `scripts/run_backlog_worker_v3.py`: deterministic Discord API backlog worker.
- `scripts/audit_caught_up_v3.py`: full live probe and optional requeue.
- `scripts/audit_discord_inventory_v3.py`: compare visible text channels plus active/archived threads against state by stable ID.
- `scripts/check_daily_sync_gate.py`: fail closed when the shared backup lock is busy or today's deterministic inventory report is missing/incomplete.
- `scripts/reconcile_raw_archive_v3.py`: compare every state entry with raw Markdown and optionally re-fetch full Discord history, appending only message IDs not already verifiably archived.
- `scripts/weekly_raw_reconcile_v4.py`: recovery-first weekly repair with targeted append-only writes, full-inventory closeout, local-only classification, and report-channel self-drift protection.
- `scripts/audit_cron_tooling.py`: reject legacy `payload.toolsAllow`; shell jobs must remove the field with `--clear-tools` and pass an isolated GPT/Codex bash canary.
- `scripts/snapshot_deployment_assets.py`: checksum customer adapters, wrappers, classification config, and mapping ledgers, then verify them with an isolated restore canary.
- `scripts/package_skill.py`: build `.skill` artifact.
- `scripts/run_lancedb_incremental.py`: run optional LanceDB incremental indexing after backup.
- `scripts/backup_workspace_assets.py`: checksummed, explicit-scope snapshots for recovery-critical workspace folders and local knowledge indexes.

## References

- Use `prompts/core-backup.md` for the customer-safe core workspace backup cron prompt.
- Read `references/architecture.md` for system design.
- Read `references/state-schema.md` before editing state/queue format.
- Read `references/customer-install.md` for installation and cron setup.
- Read `references/recovery.md` for restore/migration.
- Read `references/troubleshooting.md` for known failure modes.
- Read `references/llm-handoff.md` when another model needs to operate this skill.
- Read `references/lancedb-integration.md` when enabling knowledge indexing.

## Exclusion and customer-path rules

- `backupExcluded=true`, `invalidChannel=true`, or `syncStatus=excluded` is terminal. Selector, worker, audit, and raw reconcile skip the entry; active queue items become `invalid` with attempts reset to zero.
- Stable Discord channel/thread ID is the mapping key. Preserve existing customer `relativePath` values. New registration requires a mapping ledger, safe-path validation, and collision resolution before apply.
- Customer adapters and their mapping/classification evidence are recovery assets. Snapshot and restore-canary them before any compatibility migration.

## LLM compatibility rule

This skill must be understandable by Claude Opus, Sonnet, GPT, Gemini, and future OpenClaw-supported models.

Use plain explicit instructions, fixed status/reason enums, exact commands, and deterministic scripts. The LLM should run commands and summarize results, not reason out backup state transitions from scratch.

## Recommended install order

For customers who need searchable project memory, install `openclaw-lancedb-knowledge` first, then install this backup skill. This backup skill can call the existing LanceDB incremental index after backup jobs finish.

Keep core backup, discovery, daily sync, audit, and LanceDB outside the backlog
window. The default 23:10–04:10 window ends before the 05:15 daily pipeline and is
the production topology unless the customer explicitly approves another low-traffic
window. Manual incident runs remain bounded and do not change the recurring cron.

## Customer backup root rule

- Fresh default install: `~/Desktop/<Discord伺服器名稱>資料備份`.
- Discord archive/config root: `<backup root>/Discord資料`.
- Core workspace root passed to `core_workspace_backup.py`: `<backup root>`; the
  existing engine creates `核心文件/latest` and `核心文件/snapshots` beneath it.
- The Desktop directory is real, not a symlink or Finder alias.
- A supplied `--backup-root` wins and prevents a second Desktop root.
- Never move an existing configured root automatically. Report that migration is
  required and leave customer data unchanged.
