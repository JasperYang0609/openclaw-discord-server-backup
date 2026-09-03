# Customer Install

## Steps

0. If searchable knowledge is required, install and baseline `openclaw-lancedb-knowledge` first.
1. Clone the GitHub repository.
2. Run the installer with Python 3 and the exact Discord server display name:
   `python3 skill/openclaw-discord-server-backup/scripts/install.py --workspace ~/.openclaw/workspace --server-name "南方"`.
   A fresh default install creates the real directory
   `~/Desktop/南方資料備份/Discord資料`. For an explicitly approved custom location,
   use `--backup-root /absolute/path`; this prevents creation of a second Desktop root.
3. Set the customer config values.
4. Resolve `{{WORKSPACE_ROOT}}` and `{{BACKUP_ROOT}}` in `prompts/core-backup.md`.
   Use the installer's absolute `backupRoot` output (for example
   `/Users/customer/Desktop/南方資料備份`), not the Discord-only `Discord資料/` path.
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

The installer creates only the root and `Discord資料/` scaffolding. The existing
core-backup cron runs `core_workspace_backup.py` and then creates/verifies
`核心文件/latest/` plus one immutable `核心文件/snapshots/YYYY-MM-DD/` per day.
Do not replace that engine with prompt-written copy commands.

## Required config

- `guildId`
- `backupRoot`
- `statePath`
- `queuePath`
- `reportChannel`
- `timezone`
- read/write limits
- backlog timezone and the night-only schedule `10 0,1,2,3,4,23 * * *`

`backupRoot` in the Discord config is the absolute `.../Discord資料` directory.
The core-backup job separately receives its parent customer root.

## After install

Run healthcheck and confirm:

- state JSON loads
- queue JSON loads
- backup root exists
- the Desktop root is a real directory named `<Discord伺服器名稱>資料備份`
- Discord config `backupRoot` and state `rootPath` are identical absolute
  `<customer root>/Discord資料` paths
- core backup writes under the same customer root at `核心文件/latest/` and
  `核心文件/snapshots/`, with manifest verification and restore canary passing
- active queue count is expected
- no recurring backlog cron runs after 04:10 or during daytime; unfinished debt is
  preserved for the next night's bounded runs
- audit can probe entries
- daily-sync preflight passes with today's deterministic inventory and an available shared lock
- guild inventory reports zero missing state entries (or documents permission warnings)
- raw reconciliation reports zero missing current Discord messages
- LanceDB source map matches the required search depth: summary-only or summary + raw
- LanceDB database, metadata rules, source map, index state, and embedding cache have a verified recovery snapshot
