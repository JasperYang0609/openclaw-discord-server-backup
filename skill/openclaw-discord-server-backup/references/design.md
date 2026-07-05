# Design Reference — V3 Queue-backed Chat Backup

## Goal

Guarantee eventual completeness for Discord-style channels and threads even when a channel produces 200+ messages/day.

The system may be temporarily behind, but it must not silently mark unfinished work as complete.

## Architecture

### 1. State is the cursor ledger

Recommended top-level shape:

```json
{
  "version": 3,
  "schema": "channel-backup-state-v3",
  "guildId": "GUILD_ID",
  "rootPath": "/path/to/backup/root",
  "queuePath": "memory/channel_backup_backlog_queue.json",
  "cursorPolicy": {
    "sourceOfTruth": "lastWrittenMessageId",
    "compatField": "lastMessageId",
    "completeWhen": "message read after cursor returns 0",
    "neverAdvancePastUnwrittenRaw": true
  },
  "entries": {}
}
```

Recommended entry:

```json
{
  "type": "channel|thread",
  "channelId": "1493235887906099340",
  "parentChannel": "openclaw優化研究",
  "relativePath": "openclaw優化研究/IG finder",
  "lastWrittenMessageId": "1493073514205413476",
  "lastMessageId": "1493073514205413476",
  "lastSeenMessageId": null,
  "syncStatus": "healthy",
  "backlogReason": null,
  "lastBackup": "2026-05-17",
  "lastSuccessfulWriteAt": "2026-05-17T04:30:00Z",
  "consecutiveErrors": 0,
  "guildId": "GUILD_ID"
}
```

### 2. Queue is the work ledger

Queue file:

```json
{
  "version": 1,
  "items": [
    {
      "entryKey": "channel/thread",
      "channelId": "...",
      "relativePath": "channel/thread",
      "cursorMessageId": "...",
      "targetHintMessageId": null,
      "priority": 10,
      "reason": "page_limit_reached",
      "status": "queued",
      "attempts": 0,
      "createdAt": "ISO",
      "updatedAt": "ISO"
    }
  ]
}
```

A queue item is removed or marked `caught_up` only after a read after its cursor returns 0.

## Job responsibilities

### Discovery
- channel-list / thread-list
- create folders
- create state entries
- no message reads

### Daily incremental sync
- reads only healthy entries
- small page budget
- writes raw first, summary second, state last
- on limit/timeout, queues the entry

### Backlog worker
- queue-first selection
- bounded batches
- writes raw/summary and advances cursor after every successful batch
- keeps queue active until a zero-message after-cursor verification

### Audit/reconciliation
- the backlog worker emits `auditWarnings` every run: active queue items
  (`status in [queued, catching_up, retry]`) with `attempts > 5`, and entries
  with `consecutiveErrors > 3`
- `attempts` counts the current catch-up cycle only: it is reset to 0 when an
  item is marked `caught_up` and starts from 0 when a caught_up item is
  reactivated, so historical attempts never keep the warning permanently on
- a non-empty `auditWarnings` means an active catch-up is stuck or keeps
  erroring; escalate for human attention
- the standalone audit (`audit_caught_up_v3.py`) additionally probes every
  registered entry live and requeues false-healthy entries

## Completion and partial semantics

Complete:
- `after=<cursor>` returns 0

Partial:
- message/page cap reached
- timeout approaching
- write succeeded for only part of available stream
- bootstrap did not cover intended history

Required wording:
- `增量部分完成，剩餘交給 backlog worker`
- `首次全量備份（部分完成）`
- `backlog catch-up partial; queue remains active`

## Raw / summary output

Raw is append-only chronological evidence. Summary is a day-level operational digest.

Never overwrite raw/summary files during normal sync. Append blocks and preserve third-party/manual notes.

## High-volume entries

Mark hot entries by priority rather than bypassing safety:
- lower queue priority number
- larger worker budget if needed
- optional extra mini-sync windows

Examples: `寫code`, `VASO`, incident/monitoring threads.
