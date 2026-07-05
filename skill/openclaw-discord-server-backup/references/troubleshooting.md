# Troubleshooting

## `lastBackup` is today but messages remain

This is false healthy. Run audit and requeue with reason `live_probe_found_new_messages`.

## Queue item never finishes

Check read errors, permissions, channel access, and rate limits. Keep status as `retry` until a successful run catches up. The worker surfaces these as `auditWarnings` once an active item exceeds 5 attempts or an entry exceeds 3 consecutive errors.

## State or queue JSON is corrupt

The worker writes atomically (`.tmp` + rename) and keeps rotated `.bak` files. On a corrupt main file it automatically loads the newest parseable `.bak` and reports the source under `recoveredFrom` in its output JSON. If both the main file and every `.bak` fail to parse, the worker exits non-zero; restore from backups manually.

## Worker reports `{"skipped": "locked"}`

Another backup job holds the lockfile (`.channel_backup.lock` next to the state file). This is normal overlap protection; the next scheduled run continues. If it persists, check for a hung job before removing the lockfile.

## Persistent Discord 429 rate limits

The worker and audit retry a 429 at most 8 times, then fail that entry through the normal error path (`status=retry`, `consecutiveErrors` incremented) so one throttled channel cannot block the whole run.

## State cursor is ahead of raw

Unsafe. Roll back to the latest message id verified in raw and enqueue with reason `cursor_mismatch`.

## Thread or channel renamed

Do not overwrite old folders. Record rename history or normalize the path explicitly.

## Path collision

Add a stable channel/thread id suffix to the path. Never merge two channel ids into one folder automatically.
