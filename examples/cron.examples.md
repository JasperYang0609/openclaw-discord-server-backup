# OpenClaw Cron Examples

Use these as templates. Replace paths and report targets with customer config.

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

## LanceDB incremental indexing

Schedule: after backup and audit, for example daily 06:30.

The prompt should run `scripts/run_lancedb_incremental.py` with the customer config.

Recommended customer flow: install and baseline `openclaw-lancedb-knowledge` first, then enable this backup skill's LanceDB post-backup indexing.
