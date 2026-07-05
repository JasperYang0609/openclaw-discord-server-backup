# State Schema

## Entry status enum

- `healthy`
- `partial`
- `queued`
- `catching_up`
- `retry`
- `caught_up`
- `error`

## Queue item status enum

- `queued`
- `catching_up`
- `retry`
- `caught_up`
- `retired` — orphan item whose state entry no longer exists; kept for history and never counted as active

`attempts` counts the current catch-up cycle only: the worker resets it to 0 when an item is marked `caught_up`, and a reactivated caught_up item starts again from 0.

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
