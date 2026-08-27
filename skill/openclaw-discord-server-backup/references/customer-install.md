# Customer Install

## Steps

0. If searchable knowledge is required, install and baseline `openclaw-lancedb-knowledge` first.
1. Clone the GitHub repository.
2. Run the installer with Python 3.
3. Set the customer config values.
4. Resolve `{{WORKSPACE_ROOT}}` and `{{BACKUP_ROOT}}` in `prompts/core-backup.md`.
5. Create OpenClaw cron jobs from the prompt files and `examples/cron.examples.md`.
   Set the recurring backlog worker to `10 0,1,2,3,4,23 * * *` in the customer
   timezone. This means 23:10 and 00:10–04:10 only; routine 06:10, 11:10, and
   17:10 runs are not allowed.
6. Run backlog worker dry-run.
7. Run audit dry-run.
8. Run the guild inventory audit and resolve every `missingFromState` entry.
9. If exact Discord text must be searchable, add raw archive Markdown to the LanceDB source map and reindex.
10. Create a recovery-assets snapshot that includes the local LanceDB project and selected workspace records.
11. Enable cron jobs.

## Required config

- `guildId`
- `backupRoot`
- `statePath`
- `queuePath`
- `reportChannel`
- `timezone`
- read/write limits
- backlog timezone and the night-only schedule `10 0,1,2,3,4,23 * * *`

## After install

Run healthcheck and confirm:

- state JSON loads
- queue JSON loads
- backup root exists
- active queue count is expected
- no recurring backlog cron runs after 04:10 or during daytime; unfinished debt is
  preserved for the next night's bounded runs
- audit can probe entries
- guild inventory reports zero missing state entries (or documents permission warnings)
- raw reconciliation reports zero missing current Discord messages
- LanceDB source map matches the required search depth: summary-only or summary + raw
- LanceDB database, metadata rules, source map, index state, and embedding cache have a verified recovery snapshot
