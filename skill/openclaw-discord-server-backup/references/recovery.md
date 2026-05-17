# Recovery Reference — V3

## Restore order

1. workspace core files
2. `memory/`
3. backup tree on disk
4. `memory/channel_backup_summary_state.json`
5. `memory/channel_backup_backlog_queue.json`
6. cron definitions

State without queue can still run, but partial work may be hidden. Rebuild queue from state with `migrate_state_v3.py`.

## After restore

Run:

```bash
python3 skills/channel-backup-state-backlog/scripts/migrate_state_v3.py   --state memory/channel_backup_summary_state.json   --queue memory/channel_backup_backlog_queue.json   --backup

python3 skills/channel-backup-state-backlog/scripts/select_backlog_candidates.py   --state memory/channel_backup_summary_state.json   --queue memory/channel_backup_backlog_queue.json   --today YYYY-MM-DD   --limit 4
```

## Health checks

- JSON loads cleanly
- every entry has `lastWrittenMessageId`
- `lastMessageId == lastWrittenMessageId`
- partial/queued entries appear in queue
- daily sync prompt mentions queue
- backlog prompt says completion requires `after cursor` returns 0

## Common failure modes

### `lastBackup` says today but messages remain

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
