# Customer Install

## Steps

0. If searchable knowledge is required, install and baseline `openclaw-lancedb-knowledge` first.
1. Clone the GitHub repository.
2. Run the installer with Python 3.
3. Set the customer config values.
4. Resolve `{{WORKSPACE_ROOT}}` and `{{BACKUP_ROOT}}` in `prompts/core-backup.md`.
5. Create OpenClaw cron jobs from the prompt files and `examples/cron.examples.md`.
6. Run backlog worker dry-run.
7. Run audit dry-run.
8. Enable cron jobs.

## Required config

- `guildId`
- `backupRoot`
- `statePath`
- `queuePath`
- `reportChannel`
- `timezone`
- read/write limits

## After install

Run healthcheck and confirm:

- state JSON loads
- queue JSON loads
- backup root exists
- active queue count is expected
- audit can probe entries
