# Daily Sync V3 Prompt

Goal: run bounded daily sync for healthy Discord backup entries.

Rules:
- Do not process entries already in active queue.
- Do not process entries with `syncStatus` partial, queued, catching_up, retry, or error.
- Read after `lastWrittenMessageId` only.
- If the entry hits the configured limit, write retrieved messages, update cursor only to written raw data, mark partial, and enqueue.
- Never treat `lastBackup` as completion proof.
- If `lastBackup` is outside the daily-sync freshness window, skip it only because the backlog worker owns stale probes. Do not mark it complete.

If deterministic daily sync script is available, run it. Otherwise follow these rules exactly and keep the run small.

Report checked entries, written entries, queued entries, and errors.
