# Daily Sync V3 Prompt

Goal: run bounded daily sync for healthy Discord backup entries.

Concurrency contract:
- All daily-sync cron slots for one backup installation must use the same OpenClaw custom session key so normal runs and restart catch-up runs serialize.
- At the start of every slot, reload the current state and queue from disk. Never reuse a candidate list or cursor from an earlier slot or earlier conversation context.
- If another slot has already advanced an entry, use the newer durable cursor and select the next eligible entry instead of appending the same batch twice.
- Before selecting entries, run `{{SKILL_ROOT}}/scripts/check_daily_sync_gate.py --state "{{STATE_PATH}}" --inventory "{{INVENTORY_REPORT}}" --today <TODAY_IN_CONFIGURED_TIMEZONE> --timezone <CONFIGURED_TIMEZONE> --compact`. If the shared lock is busy or today's inventory is missing/incomplete, make no changes.
- This is an internal component job. Do not call the OpenClaw `message` tool, do not send Discord messages or status cards, and do not expose per-entry cursor/added/processed details in the final response. Cron delivery is intentionally `none`; only the separate 07:05 health-report job is user-facing. Execution failures are handled by the owned cron failure alert.

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

Component receipt contract:
- Always finish by atomically recording `backup-health-component.v1` through `{{SKILL_ROOT}}/scripts/backup_health_report.py record` with receipt root `{{RECEIPT_DIR}}`, component `{{COMPONENT}}`, declaration key `{{DECLARATION_KEY}}`, and producer `openclaw-discord-server-backup/daily-sync-v1`.
- Healthy completion uses status `ok` and a plain summary such as `本輪日常同步已完成`.
- If the Gate reason is `backup_lock_busy`, use status `warning`, summary `另一個備份流程仍在執行，本輪已安全略過；資料沒有被覆蓋，下一輪會續做`, anomaly code `backup_lock_busy`, impact `本輪新訊息備份延後`, dataLoss `no`, repairStatus `保留原游標並等待下一輪`, and pending `等待下一輪自動續做`.
- A missing, stale, incomplete, or warning-bearing inventory uses status `warning`, explains that synchronization was safely blocked before any read/write, and leaves a pending item for the next slot.
- A real execution error uses status `error`; never convert it to a successful skip.
- Keep checked/written/queued/error counts only in the receipt metrics and local run transcript. The final internal response is one short sentence without message IDs or file paths.
