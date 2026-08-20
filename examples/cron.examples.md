# OpenClaw Cron Examples

Use these as templates. Replace paths and report targets with customer config.

## Core workspace backup

Schedule: daily 05:15, before Discord discovery and sync.

Use `skill/openclaw-discord-server-backup/prompts/core-backup.md`. Replace
`{{WORKSPACE_ROOT}}` and `{{BACKUP_ROOT}}` with customer-specific absolute paths.
The job invokes `scripts/core_workspace_backup.py`, refreshes a staged and manifest-verified `核心文件/latest/` every run, creates at most one immutable `核心文件/snapshots/YYYY-MM-DD/` snapshot per local calendar day, and runs an isolated restore canary. It discovers root-level Markdown files dynamically and backs up the entire `memory/` folder; do not replace the engine with prompt-written copy logic or a hardcoded filename list.

## Discovery

Schedule: daily 05:25.

Discovery only registers channels/threads and creates folders. It must not read messages.

## Daily sync

Schedule: daily 05:30.

The prompt should follow `prompts/daily-sync-v3.md` with the V3 hard limits (30/60/4):
`limit=30` per read (exactly 30 allows one extra page), at most 60 messages written
per entry per run, at most 4 entries written per run (at most 6 checked). On any cap:
write first, advance cursor only to written raw, mark `partial`, enqueue backlog.

## Backlog worker

Schedule: `10 0,1,2,3,4,6,11,17,23 * * *`.

The prompt should run the deterministic worker with config-derived paths, including the queue path:

```
python3 scripts/run_backlog_worker_v3.py \
  --state <STATE_PATH> \
  --queue <QUEUE_PATH> \
  --root <BACKUP_ROOT> \
  --today <YYYY-MM-DD> \
  --max-entries 4 --max-batches 12 --max-batches-per-entry 5 --limit 100
```

Completion rule: a queue item becomes `caught_up` only when a read `after=<cursor>`
returns 0 messages. Never use `lastBackup` to decide completion. Report
`processed` / `totalBatches` / `activeQueueLeft` / `auditWarnings` from the worker JSON.

## Audit

Schedule: daily 06:30 or 23:30.

The prompt should run `scripts/audit_caught_up_v3.py` and report any false healthy entries.
The backlog worker also emits `auditWarnings` every run for stuck active catch-ups.

## Full raw integrity reconcile

Schedule: weekly during a low-traffic window, for example Sunday 14:10.

Run `scripts/reconcile_raw_archive_v3.py --apply --compact` with the customer state, queue, and channel archive root. This heavier scan starts from the oldest current Discord history, verifies message IDs against raw Markdown, preserves existing files, and appends only messages that are not already verifiably archived.

This job complements the normal backlog worker. `after=<cursor>` proves only that no newer message remains; the weekly reconcile proves the historical raw archive itself contains the current Discord history.

## Full guild inventory audit

Schedule: before the weekly raw reconcile, for example Sunday 13:50.

Run `scripts/audit_discord_inventory_v3.py` with the guild ID and state path. The audit compares stable IDs for visible text channels plus active and archived threads. Treat `missingFromState > 0` as a backup coverage failure; register those entries before claiming the server is complete. Archived private-thread endpoints can return permission warnings, which must remain visible in the report.

## Workspace recovery assets

Schedule: weekly after LanceDB indexing and backup verification.

Run `scripts/backup_workspace_assets.py --apply` with an explicit list of recovery-critical folders. Recommended examples are records, scripts, skills, hooks, reports, handoff files, and the local LanceDB project. Keep large media, model, build, dependency, log, and temporary directories outside this job unless the customer explicitly chooses their storage and retention policy.

## LanceDB incremental indexing

Schedule: after backup and audit, for example daily 06:30.

The prompt should run `scripts/run_lancedb_incremental.py` with the customer config.

Recommended customer flow: install and baseline `openclaw-lancedb-knowledge` first, then enable this backup skill's LanceDB post-backup indexing.

If exact Discord wording, examples, or chronology must be searchable, add a separate source-map entry for `**/raw/**/*.md`; the summary-only default intentionally does not index raw chat. Back up the LanceDB database, index state, configuration, metadata rules, and embedding cache as recovery assets because deterministic tags are stored on chunk rows inside the local database.
