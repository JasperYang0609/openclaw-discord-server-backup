# Changelog

## Unreleased

- Serialize daily-sync cron slots through one custom session and require every slot to reload durable state/queue before candidate selection, preventing restart catch-up races and duplicate batch selection.
- Add a deterministic daily-sync preflight gate that skips safely while another backup job holds the shared lock or today's inventory audit is missing/incomplete.
- Freeze the audited report entry at a message-ID cutoff during weekly V4 reconciliation so progress cards belong to the next incremental scope instead of causing closeout drift.
- Share remaining backlog safety-net slots between stale probes and null-cursor bootstrap entries while keeping excluded/invalid entries terminal.

- Make fresh macOS installs create one real Desktop directory named
  `<Discord伺服器名稱>資料備份`, with `Discord資料/` for Discord archives and the
  existing core-backup engine owning `核心文件/latest` and immutable daily
  snapshots. Config/state paths now match, unsafe names and destinations fail
  closed, custom roots remain supported, and existing trees are never moved silently.

- Move the default backlog worker to a night-only 23:10–04:10 window, removing
  routine 06:10, 11:10, and 17:10 runs so catch-up work does not contend with
  interactive or daily backup tasks; bounded limits and durable next-night resume
  remain unchanged.
- Harden excluded/invalid entries across selector, worker, audit, and raw reconciliation; active retry items are retired as `invalid` instead of returning to backlog.
- Add recovery-first weekly full-inventory raw repair with bounded append-only closeout, local-only ID classification, and report-channel self-drift protection.
- Add stable-ID mapping ledgers that preserve customer paths and block unsafe/colliding registration before apply.
- Add deployment-customization recovery bundles with SHA-256 verification and isolated restore canaries.
- Add cron tooling preflight that rejects legacy `payload.toolsAllow`, documents `--clear-tools`, and requires a temporary GPT/Codex isolated bash canary.

- Make `post_run_check.py` layout-aware so it performs the full repository checks in a clone and real Python/CLI smoke checks when executed from an installed or extracted `.skill` package.
- Report active and archived thread counts separately; fail the inventory completeness gate and return an unknown archived total when any archived endpoint is blocked or pagination is incomplete.
- Add full guild inventory auditing for visible text channels and active/archived threads so entries missing from state cannot be mistaken for a complete server backup.
- Add explicit-scope, checksummed workspace recovery snapshots for local LanceDB data, deterministic metadata/tag rules, records, and other selected operational assets.
- Add a raw archive reconciliation tool that detects empty archives, cursor-ahead-of-raw state, duplicate raw message IDs, and missing current Discord history; authorized repair preserves existing files and appends only unarchived message IDs.

- Replace prompt-only core workspace copying with a deterministic staged backup engine, exact SHA-256 manifests, immutable daily snapshots, tamper/extra/missing detection, and an isolated restore canary.
- Add a customer-safe core workspace backup prompt with runtime Markdown discovery, full `memory/` backup, immutable daily snapshots, cron guidance, and package inclusion.
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
