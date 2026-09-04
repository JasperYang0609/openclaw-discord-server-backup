# Customer Install

## Steps

0. If searchable knowledge is required, install and baseline `openclaw-lancedb-knowledge` first.
1. Clone the GitHub repository.
2. Run the installer with Python 3, the exact Discord server display name, stable
   guild ID, explicit report target, agent, and timezone:
   `python3 skill/openclaw-discord-server-backup/scripts/install.py --workspace ~/.openclaw/workspace --server-name "南方" --guild-id "GUILD_ID" --report-to "channel:REPORT_ID" --agent main --timezone Asia/Taipei`.
   A fresh default install creates the real directory
   `~/Desktop/南方資料備份/Discord資料`. For an explicitly approved custom location,
   use `--backup-root /absolute/path`; this prevents creation of a second Desktop root.
3. Let the installer render, stage, canary-test, enable, and exactly verify all 11
   owned jobs. A successful normal run reports `READY`; an identical rerun changes
   nothing. Do not edit generated prompts or create cron jobs manually.
4. Run the installed `scripts/post_run_check.py` and retain the transaction receipt.
5. Confirm the first 07:05 report. Weekly/monthly items remain `尚未到首次驗證`
   until their actual jobs create verified evidence.

The installer creates the root, `Discord資料/` scaffolding, private receipts, and
the owned cron topology. The core-backup cron runs `core_workspace_backup.py` and then creates/verifies
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
- `agentId` and private `receiptDir`
- explicit workspace snapshot includes that already exist; missing includes block installation

## Offline and legacy upgrade rules

- `--offline-scaffold` is explicitly incomplete and returns
  `PARTIAL_MANUAL_ACTION`; do not label it ready or enable jobs later without rerunning
  the normal installer.
- Existing configured backup roots are preserved exactly, including legacy
  `頻道紀錄` layouts. Raw customer files are hashed before/after and never migrated
  implicitly.
- Unknown or look-alike cron jobs block the transaction and remain untouched.
- An allowlisted legacy job may be disabled only through an explicit adoption map
  containing its exact job ID and current fingerprint. The manager additionally
  verifies role marker, schedule, timezone, payload kind, and legacy declaration key.
- The transaction receipt contains the complete rollback plan. Any failure after
  cron commit invokes receipt-backed rollback before restoring files.

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
- exactly 11 owned declaration keys exist, with no duplicates, and every job matches name, schedule, timezone, kind, delivery, timeout, alert, and session contract
- the three daily-sync jobs share one persistent session
- only the 07:05 health job announces routine output
- a deliberately stale or malformed receipt makes the report non-green
