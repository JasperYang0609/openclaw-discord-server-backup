# Backlog Worker V3 Prompt

Goal: run the deterministic backlog worker for this OpenClaw Discord backup skill.

Inputs:
- customer config JSON
- state path from config (`statePath`)
- queue path from config (`queuePath`)
- backup root from config (`backupRoot`)

Action:
1. Read the customer config.
2. Run the worker with the configured paths and limits (today = current date in the configured timezone):

   `python3 scripts/run_backlog_worker_v3.py --state <STATE_PATH> --queue <QUEUE_PATH> --root <BACKUP_ROOT> --today <YYYY-MM-DD> --max-entries 4 --max-batches 12 --max-batches-per-entry 5 --limit 100`

3. Summarize the JSON result.

Do not manually update state or queue. Do not rewrite the worker logic inline.

Completion rule: only the script may mark queue items `caught_up`, and only after an after-cursor read returns 0.

Safety rules:
- The worker must probe bounded `healthy` entries whose `lastBackup` is stale, even when they are not already in queue. Use reason `healthy_stale_probe`.
- The queue cursor must never lag behind the state cursor; use the newest durable cursor to avoid duplicate raw appends.
- Only enqueue the stale entries selected for the current bounded run; do not flood the queue with every stale healthy entry.
- Entries with null cursor must be handled as bounded bootstrap work with reason `bootstrap_needed`; they must not be skipped forever. The worker probes each of them at most once per `--today` date.
- If the worker prints `{"skipped": "locked"}`, another backup job holds the lockfile; report the skip and stop instead of retrying immediately.

Report (from worker JSON):
- `processed` / `totalBatches` / `activeQueueLeft`
- `auditWarnings`: non-empty means an active catch-up is stuck (`attempts > 5`) or an entry keeps erroring (`consecutiveErrors > 3`); flag it for human attention
- `recoveredFrom` if present: the worker recovered a corrupt state/queue file from a `.bak`; report it
- each entry status, added count, and cursor
- if `activeQueueLeft > 0`, say the next run will continue
