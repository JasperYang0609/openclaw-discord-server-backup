# State Schema

## Entry status enum

- `healthy`
- `partial`
- `queued`
- `catching_up`
- `retry`
- `caught_up`
- `error`

## Backlog reason enum

- `page_limit_reached`
- `daily_deferred`
- `bootstrap_needed`
- `live_probe_found_new_messages`
- `cursor_mismatch`
- `read_error`

## Entry fields

- `type`
- `channelId`
- `relativePath`
- `parentChannel`
- `lastWrittenMessageId`
- `lastMessageId`
- `lastSeenMessageId`
- `syncStatus`
- `backlogReason`
- `lastBackup`
- `lastSuccessfulWriteAt`
- `consecutiveErrors`

## Queue fields

- `entryKey`
- `channelId`
- `relativePath`
- `cursorMessageId`
- `targetHintMessageId`
- `priority`
- `reason`
- `status`
- `attempts`
- `createdAt`
- `updatedAt`
