# OpenClaw Cron Examples

This file documents the installer-owned topology. For a normal installation, run
`scripts/install.py`; do not hand-create these jobs. The canonical machine-readable
source is `manifests/owned-cron.v1.json`, shipped inside the `.skill` package.

The installer validates the full cron inventory, stages all desired jobs disabled,
runs an isolated command canary and a two-job shared-session serialization canary,
then enables and verifies the complete set. Exact reruns are no-ops. Unknown jobs
are never deleted; adoption requires an explicit checksummed map.

## Core workspace backup

Schedule: daily 05:10, before Discord discovery and sync.

Use `skill/openclaw-discord-server-backup/prompts/core-backup.md`. Replace
`{{WORKSPACE_ROOT}}` and `{{BACKUP_ROOT}}` with customer-specific absolute paths.
For the default installer layout, `{{BACKUP_ROOT}}` is the real Desktop customer root
reported by the installer, for example `/Users/customer/Desktop/南方資料備份`; do
not pass the Discord-only `Discord資料/` child to the core backup engine.
The job invokes `scripts/core_workspace_backup.py`, refreshes a staged and manifest-verified `核心文件/latest/` every run, creates at most one immutable `核心文件/snapshots/YYYY-MM-DD/` snapshot per local calendar day, and runs an isolated restore canary. It discovers root-level Markdown files dynamically and backs up the entire `memory/` folder; do not replace the engine with prompt-written copy logic or a hardcoded filename list.

## Discovery

Schedule: daily 05:25.

Discovery only registers channels/threads and creates folders. It must not read messages.

## Daily sync

Schedules: daily 05:30, 05:40, and 05:50.

The prompt should follow `prompts/daily-sync-v3.md` with the V3 hard limits (30/60/4):
`limit=30` per read (exactly 30 allows one extra page), at most 60 messages written
per entry per run, at most 4 entries written per run (at most 6 checked). On any cap:
write first, advance cursor only to written raw, mark `partial`, enqueue backlog.

All daily-sync slots for one install must share one custom session key. Before entry
selection, run `scripts/check_daily_sync_gate.py` against the state and today's
deterministic inventory report. A busy shared lock or stale/incomplete inventory is a
safe `skipped` run: do not read messages, write files, or advance cursors.

## Backlog worker

Schedule: `10 0,1,2,3,4,23 * * *` (Asia/Taipei).

This is a night-only catch-up window: one bounded run at 23:10, then hourly from
00:10 through 04:10. Do not add 06:10, 11:10, or 17:10 routine runs. Keeping
backlog work out of daytime avoids competing with interactive OpenClaw tasks, and
stopping before 05:00 leaves a clean buffer before core backup, discovery, daily
sync, audit, and LanceDB jobs. If the queue remains active after 04:10, preserve it
for the next night instead of increasing batch limits or starting a daytime worker.

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

An operator may run the same bounded worker manually for an incident, but routine
customer cron definitions must keep the night-only schedule above.

## Audit

Schedule: daily 06:10.

The prompt should run `scripts/audit_caught_up_v3.py` and report any false healthy entries.
The backlog worker also emits `auditWarnings` every run for stuck active catch-ups.

## Full raw integrity reconcile

Schedule: weekly during a low-traffic window, for example Sunday 14:10.

Run `scripts/weekly_raw_reconcile_v4.py --compact` with the customer state, queue, channel archive root, and a new evidence directory. It performs a full comparison, creates recovery copies before the first append, applies only missing message IDs, repeats bounded closeout passes, and finishes with full-inventory/local-only classification. It never deletes or rewrites existing raw.

If the report channel is part of inventory, pass `--report-entry-key`. Send progress before starting the command, then send nothing to that channel until the command exits and records `capturedAt`; otherwise the status message itself becomes new live-only drift.

This job complements the normal backlog worker. `after=<cursor>` proves only that no newer message remains; the weekly reconcile proves the historical raw archive itself contains the current Discord history.

## Full guild inventory audit

Schedule: before the weekly raw reconcile, for example Sunday 13:50.

Run `scripts/audit_discord_inventory_v3.py` with the guild ID, state path, archive root, and `--mapping-ledger-out`. The audit compares stable IDs for visible text channels plus active and archived threads. Existing customer paths are preserved; new entries are planned with safe-path and collision gates. `--apply` is rejected unless a mapping ledger is written and contains no blocker. Treat `missingFromState > 0` as a backup coverage failure. Archived private-thread endpoint warnings must remain visible.

## Cron tooling preflight

Before enabling shell-dependent GPT/Codex cron jobs, pipe `openclaw cron list --all --json` into `scripts/audit_cron_tooling.py`. Any `payload.toolsAllow` field is a blocker, including `toolsAllow: []`. For an `agentTurn` job, remove the field with `openclaw cron edit <job-id> --clear-tools`. For a command job, stop before mutation and use a reviewed transaction that stages a freshly rendered declaration without `toolsAllow`; never apply a one-off tool-list edit to a command payload. After repair, run a temporary isolated canary that executes `pwd && echo TOOL_OK`; remove the canary after `TOOL_OK` is observed.

## Daily consolidated health report

Schedule: daily 07:05.

Routine components use silent delivery and write owner-only structured receipts.
The health job first verifies the owned topology, then announces one Traditional
Chinese report. Current-day daily and Qwen evidence is required; weekly and monthly
evidence uses its own cadence. A newly installed job that has not reached its first
scheduled run is shown as `需注意／已安裝，尚未到首次驗證`, never as passed.

## Workspace recovery assets

Schedule: day 1 of each month at 07:00.

Run `scripts/backup_workspace_assets.py --apply` with an explicit list of recovery-critical folders. Recommended examples are records, scripts, skills, hooks, reports, handoff files, and the local LanceDB project. Keep large media, model, build, dependency, log, and temporary directories outside this job unless the customer explicitly chooses their storage and retention policy.

For customer compatibility migrations, also use `scripts/snapshot_deployment_assets.py` to capture the installed skill, inventory adapter, wrapper, classification configuration, and mapping ledger. Verify the bundle and run its isolated restore canary before apply.

## LanceDB incremental indexing

Schedule: after backup and audit, for example daily 06:30.

The prompt should run `scripts/run_lancedb_incremental.py` with the customer config.

Recommended customer flow: install and baseline the separate local knowledge product
first, then pass its explicit receipt path to this installer. This backup product
does not create, remove, or guess Qwen-owned cron jobs or paths.

If exact Discord wording, examples, or chronology must be searchable, add a separate source-map entry for `**/raw/**/*.md`; the summary-only default intentionally does not index raw chat. Back up the LanceDB database, index state, configuration, metadata rules, and embedding cache as recovery assets because deterministic tags are stored on chunk rows inside the local database.
