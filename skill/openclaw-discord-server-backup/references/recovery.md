# Recovery Reference — V3

## Before any restore

Core-workspace backups are trusted only after exact manifest verification and an isolated restore canary:

```bash
python3 skills/openclaw-discord-server-backup/scripts/core_workspace_backup.py verify \
  --backup-dir "/path/to/backup/核心文件/latest"

python3 skills/openclaw-discord-server-backup/scripts/core_workspace_backup.py restore-canary \
  --backup-dir "/path/to/backup/核心文件/latest"
```

Both commands fail on missing, extra, symlinked, size-mismatched, or SHA-256-mismatched content. The canary restores only into an automatically removed temporary directory. This repository intentionally provides no command that overwrites a live workspace.

For a real recovery, stop writers first, select a verified latest or dated snapshot, copy into a separate staging workspace, inspect the diff, and only then perform a human-approved replacement. Never merge an unverified tree directly into production.

## Restore order

1. verified workspace core files
2. verified `memory/`
3. Discord backup tree on disk
4. `memory/channel_backup_summary_state.json`
5. `memory/channel_backup_backlog_queue.json`
6. cron definitions
7. customer adapter/wrapper, classification config, and stable-ID mapping ledger
8. verified owned-cron transaction receipt and topology

State without queue can still run, but partial work may be hidden. Rebuild queue from state with `migrate_state_v3.py`.

## After staged restore

Run:

```bash
python3 skills/openclaw-discord-server-backup/scripts/migrate_state_v3.py \
  --state memory/channel_backup_summary_state.json \
  --queue memory/channel_backup_backlog_queue.json \
  --backup

python3 skills/openclaw-discord-server-backup/scripts/select_backlog_candidates.py \
  --state memory/channel_backup_summary_state.json \
  --queue memory/channel_backup_backlog_queue.json \
  --today YYYY-MM-DD \
  --limit 4
```

## Health checks

- core backup manifest verifies with exact file/directory sets and SHA-256
- isolated restore canary passes
- JSON state and queue load cleanly
- every Discord entry has `lastWrittenMessageId`
- `lastMessageId == lastWrittenMessageId`
- partial/queued entries appear in queue
- daily sync prompt mentions queue
- backlog prompt says completion requires `after cursor` returns 0
- explicitly excluded/invalid entries are absent from candidates and their queue items are `invalid`
- compatibility recovery bundle verifies and passes an isolated restore canary
- cron JSON contains no `payload.toolsAllow`; shell jobs passed a temporary GPT/Codex isolated canary
- weekly pre-repair evidence verifies before any append:
  `python3 scripts/weekly_raw_reconcile_v4.py --verify-evidence /path/to/pre-repair`
- workspace recovery snapshots pass both:
  `python3 scripts/backup_workspace_assets.py verify --snapshot /path/to/snapshot`
  and `python3 scripts/backup_workspace_assets.py restore-canary --snapshot /path/to/snapshot`
- cron transaction receipts verify with
  `python3 scripts/manage_cron_topology.py verify-receipt --receipt /path/to/transaction`

## Common failure modes

### Core backup has a manifest mismatch

Treat the tree as unsafe. Do not regenerate the manifest over damaged content. Use another verified daily snapshot or create a fresh backup from the source workspace.

### Existing daily snapshot fails verification

Daily snapshots are immutable. Do not overwrite or delete it automatically. Quarantine the backup root for review and use another verified date.

### `lastBackup` says today but Discord messages remain

Cause: daily sync hit a limit and updated `lastBackup` without active queue.

Fix:
- set `syncStatus=partial`
- set `backlogReason=page_limit_reached`
- enqueue with cursor at latest written raw message

### Raw exists but state cursor is behind

Usually safe but causes duplicate reads. Repair by checking latest written message id in raw, then conservatively update state only if the raw evidence is clear.

### State cursor is ahead of raw

Unsafe. Roll state cursor back to latest verified raw message id and enqueue.

### Queue item keeps failing

Set `status=retry`, increase `attempts`, and report after threshold. Do not delete the item until caught up or explicitly retired.

### State says caught up but raw is empty or cannot prove the cursor

This is not healthy. An `after=<cursor>` probe only proves there are no newer messages; it does not prove older messages were written.

Run `scripts/weekly_raw_reconcile_v4.py`. It creates recovery evidence before append-only repair, repeats bounded closeout scans, classifies local-only IDs, and fails closed on unknown classifications or live errors. Do not emit status messages into an audited report channel during its final scan window.

### Cron upgrade failed after jobs were enabled

Do not reconstruct the old topology from memory. The installer automatically invokes
receipt-backed rollback. For an operator-authorized repeat, use
`manage_cron_topology.py rollback-receipt --receipt <transaction-directory>` and
then verify the exact restored inventory. Unknown jobs are outside product ownership
and must never be deleted as part of rollback.

### A weekly evidence bundle fails verification

Stop repair. Do not regenerate a manifest over the bundle or append any raw data.
The evidence directory is immutable and binds state, queue, and every affected raw
file by exact path, byte count, and SHA-256. Create a new uniquely named evidence
bundle only after the underlying cause is understood.

### A workspace snapshot restore canary fails

Do not advance `latest` and do not use that snapshot for recovery. Select another
verified immutable snapshot. Restore canaries always use temporary directories and
never overwrite the live workspace.
