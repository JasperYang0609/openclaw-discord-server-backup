# Backlog Worker V3 Prompt

Goal: run the deterministic backlog worker for this OpenClaw Discord backup skill.

Inputs:
- customer config JSON
- state path from config
- queue path from config
- backup root from config

Action:
1. Read the customer config.
2. Run `scripts/run_backlog_worker_v3.py` with the configured state, queue, backup root, limits, and today date.
3. Summarize the JSON result.

Do not manually update state or queue.

Completion rule: only the script may mark queue items `caught_up`, and only after an after-cursor read returns 0.

Safety rules:
- The worker must probe bounded `healthy` entries whose `lastBackup` is stale, even when they are not already in queue. Use reason `healthy_stale_probe`.
- The queue cursor must never lag behind the state cursor; use the newest durable cursor to avoid duplicate raw appends.
- Only enqueue the stale entries selected for the current bounded run; do not flood the queue with every stale healthy entry.
- Entries with null cursor must be handled as bounded bootstrap work with reason `bootstrap_needed`; they must not be skipped forever.

Report:
- processed count
- total batches
- active queue left
- each entry status and added count
