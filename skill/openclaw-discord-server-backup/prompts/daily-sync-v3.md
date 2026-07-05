# Daily Sync V3 Prompt

Goal: run bounded daily sync for healthy Discord backup entries.

Rules:
- Do not process entries already in active queue.
- Do not process entries with `syncStatus` partial, queued, catching_up, retry, or error.
- Read after `lastWrittenMessageId` only (`lastMessageId` is a compat alias kept equal to it).
- If the entry hits the configured limit, write retrieved messages, update cursor only to written raw data, mark partial, and enqueue.
- Never treat `lastBackup` as completion proof.
- If `lastBackup` is outside the daily-sync freshness window, skip it only because the backlog worker owns stale probes. Do not mark it complete.
- No bootstrap in daily sync: null-cursor entries belong to the backlog worker.

Hard limits (V3 30/60/4):
- `limit=30` per read; exactly 30 messages returned is the only case allowing one extra page.
- At most 60 messages written per entry per run (`dailyMessageLimitPerEntry`).
- At most 4 entries with actual writes per run; at most 6 entries checked (`dailyEntryLimit`); at most 180 messages read per run.

On any cap:
1. Write the retrieved raw first, then summary.
2. Advance the cursor only to the latest actually-written message ID.
3. Set `syncStatus=partial` and `backlogReason=page_limit_reached`.
4. Upsert a backlog queue item (`status=queued`, cursor = `lastWrittenMessageId`).

If deterministic daily sync script is available, run it. Otherwise follow these rules exactly and keep the run small.

Report checked entries, written entries, queued entries, and errors.
