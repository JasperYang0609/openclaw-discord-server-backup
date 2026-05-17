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

Report:
- processed count
- total batches
- active queue left
- each entry status and added count
