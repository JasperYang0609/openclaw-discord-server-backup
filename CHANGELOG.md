# Changelog

## Unreleased

- Cap Discord 429 retries at 8 in the backlog worker and audit probe, then fail the entry through the normal error path instead of blocking the run forever.
- Write state/queue JSON atomically (`.tmp` + rename) and recover corrupt files from the newest parseable `.bak`, reporting the source as `recoveredFrom` in the worker output.
- Add a cross-job `fcntl.flock` lockfile next to the state file; a second concurrent worker run exits cleanly with `{"skipped": "locked"}`.
- Merge the on-disk state before every worker save so cursors stay monotonic (larger snowflake wins) and concurrent daily-sync progress is never overwritten.
- Probe null-cursor bootstrap entries and stale-healthy entries at most once per `--today` date, using the same base date written to `lastBackup` on caught_up.
- Keep queue item priority monotonic on upsert (minimum priority number wins), matching `migrate_state_v3.merge_queue`.
- Treat `catching_up`, `error`, and `queued` as partial markers in `migrate_state_v3.py` so migration never resets unfinished backlog to healthy; already-queued entries keep their `queued` status.
- Exit non-zero when the worker is started with a missing state file instead of silently starting from empty state.
- Mark orphan queue items (state entry no longer exists) as `retired` so they stop counting toward the active queue; rotate worker `.bak` files keeping the newest 5.
- Emit `auditWarnings` from the worker for active queue items with `attempts > 5` and entries with `consecutiveErrors > 3`; attempts reset on `caught_up` and on reactivation so historical attempts cannot keep warnings permanently triggered.
- Document the engine/install layering in SKILL.md and sync prompts and cron examples to V3: daily-sync hard limits 30/60/4, full backlog worker command with queue path, and the after-cursor-returns-0 completion rule.
- Add regression tests for 429 caps, atomic save/backup recovery, merge monotonicity, priority/attempts rules, orphan retirement, lockfile skip, missing-state exit, and audit warning scoping.
- Fix backlog worker stale-healthy coverage so quiet channels that become active again are probed with `healthy_stale_probe`.
- Keep queue cursors monotonic with state cursors to prevent duplicate raw appends from stale queue items.
- Limit stale probe queue upserts to the entries selected for the current bounded run.
- Add backlog worker selection regression tests for stale probes and cursor monotonicity.
- Add bounded bootstrap selection for entries with null cursors so they do not remain permanently unprocessed.

## 1.0.0 - planned

- Initial OpenClaw Discord backup skill.
- V3 state with `lastWrittenMessageId` cursor.
- Backlog queue.
- Deterministic backlog worker.
- Full caught-up audit probe.
- Customer install/config skeleton.
- Optional LanceDB post-backup incremental indexing integration.
