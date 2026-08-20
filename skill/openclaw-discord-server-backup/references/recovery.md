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

Run `scripts/reconcile_raw_archive_v3.py` in live dry-run mode first. If the report shows missing history, rerun with `--apply`. The repair scans full current Discord history from cursor `0`, preserves existing raw files, and appends only message IDs that are not already present in recognized raw message headers.
