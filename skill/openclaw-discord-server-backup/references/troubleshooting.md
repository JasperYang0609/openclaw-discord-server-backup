# Troubleshooting

## `lastBackup` is today but messages remain

This is false healthy. Run audit and requeue with reason `live_probe_found_new_messages`.

## Queue item never finishes

Check read errors, permissions, channel access, and rate limits. Keep status as `retry` until a successful run catches up.

## State cursor is ahead of raw

Unsafe. Roll back to the latest message id verified in raw and enqueue with reason `cursor_mismatch`.

## Thread or channel renamed

Do not overwrite old folders. Record rename history or normalize the path explicitly.

## Path collision

Add a stable channel/thread id suffix to the path. Never merge two channel ids into one folder automatically.
