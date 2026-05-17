# Audit V3 Prompt

Goal: verify whether every registered channel/thread is caught up.

Action:
1. Run `scripts/audit_caught_up_v3.py` with the customer config.
2. If false healthy entries are found, requeue them only when `--requeue` is explicitly used or the cron is configured for auto-requeue.
3. Report active queue, unhealthy entries, cursor mismatch, null cursor, live new messages, and errors.

Completion: audit is clean when all result lists are empty and active queue is 0.
