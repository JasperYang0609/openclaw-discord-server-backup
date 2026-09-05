# Deterministic Daily Rich Runner v2

Status: approved for isolated implementation; live deployment is outside this branch.

## Problem

The three daily Discord backup slots must stop delegating state transitions to an
LLM prompt.  A successful API call or a locally valid generation is not enough to
advance a cursor: the runner must prove that the exact fetched source records are
present in the exact `CURRENT` generation selected after publication.  It must
also distinguish incremental freshness from full-history completeness.

## Scope

In scope:

- one deterministic command used by `daily-sync-1`, `daily-sync-2`, and
  `daily-sync-3`;
- bounded Discord reads, typed failures, deterministic candidate selection, and
  queue-first persistence;
- one module-issued shared archive lock token and one shared asset budget for the
  whole slot, across all selected entries;
- exact post-merge source/hash/CURRENT readback before cursor advancement;
- quiet-path validation of the selected rich generation;
- rotating observation refresh for existing message IDs;
- explicit rich-completeness state that cannot report full PASS after an
  incremental run;
- command-only cron topology and package/source parity tests.

Out of scope:

- full-history rich rebuild implementation;
- backlog, reconcile, or weekly writer changes;
- deleting legacy raw files or duplicate history;
- live cron mutation, deployment, Discord mutation, or Qwen restart.

## Runtime ownership

The runner obtains exactly one lock token from the rich archive module before it
loads mutable state.  Every entry store operation receives that same token.  No
boolean, caller-constructed object, alternate lock file, or implicit per-entry
lock can bypass ownership.  The runner also creates one `AssetRunBudget` and
passes the same object to every merge in the slot.

The exact lock/evidence constructors are intentionally resolved only after the
rich-core follow-up closes its independent review.  The final integration must
feature-detect an exact versioned contract and fail with
`rich_core_contract_unsupported`; it must not accept a permissive compatibility
fallback.

## Fresh inventory binding

The daily inventory report is trusted only when all of the following are true:

- it is a regular, owner-controlled JSON file with the supported schema;
- it represents the configured guild and the current local calendar date;
- enumeration is complete, has a terminal-page proof, and has no warnings or
  missing entries;
- its canonical payload digest recomputes exactly;
- every selected entry binds its channel ID and normalized relative path to one
  inventory item;
- the inventory observation time and digest are copied into the daily operation
  receipt.

Inventory values never authorize filesystem paths or executables.

## Selection and bounded reads

Entries are selected deterministically by durable scheduling fields and stable
identity.  Excluded/invalid entries and entries owned by an active queue item are
never processed by daily sync.  Null-cursor or missing-baseline entries are not
silently skipped: they are queued with `rich_full_rebuild_required`.

Per-slot limits cap selected entries, written entries, Discord messages, page
size, pages, response bytes, cumulative 429 wait, mutable-refresh messages,
asset probes, asset files, and asset bytes.  An exactly full page is not a
terminal-page proof; it remains partial until a following empty page is observed.

## Mutable observation refresh

`after=<new-message-cursor>` cannot discover edits, reactions, or pin changes on
older messages.  Each entry therefore stores a separate
`richArchiveMutableScanCursor`.  On every selected run the runner chooses a
bounded deterministic slice of canonical message IDs after that scan cursor,
wraps at the end, re-fetches those IDs using bounded Discord reads, and advances
the scan cursor only after the exact refreshed observations are verified in
`CURRENT`.

The field records scan progress, not completeness.  Until a full cycle finishes,
the entry remains `mutable_refresh_pending`; after a cycle it becomes
`mutable_refresh_stale` immediately when newer mutable state may exist.  A
scheduled full rich refresh remains the authoritative full-completeness gate.

## Merge and readback contract

For every fetched message the runner computes the canonical API-source digest
through the reviewed rich-core normalizer.  The rich merge result must match an
exact versioned schema and bind:

- entry/channel identity;
- fetched message-ID set and per-ID API-source digests;
- pre-merge and committed generation identities;
- committed generation content hash and checksummed `CURRENT` pointer;
- inventory digest and observation time;
- operation cutoff and incremental/full mode;
- shared lock-token and shared-run-budget ownership receipts.

After merge, the runner independently resolves `CURRENT`, verifies its manifest
and local receipt, reloads canonical records, and compares the exact fetched ID
set and per-ID active API-source digests.  Only then may it advance the new-message
cursor.  Generic keys such as `ok`, `verified`, or caller-supplied percentages do
not satisfy this contract.

## Quiet path

A zero-new-message response is not automatically healthy.  Before updating
`lastBackup`, the runner must resolve and verify `CURRENT`, bind the generation
to the entry identity, and confirm that the stored cursor exists in the canonical
generation (unless independently evidenced truly empty).  Missing/invalid
baseline queues `rich_full_rebuild_required`; local corruption queues
`rich_archive_repair_required`.  No quiet path may create a full PASS receipt.

## State and queue contract

Daily state uses separate dimensions:

- `syncStatus`: incremental transport/write ownership;
- `richArchiveIncrementalStatus`: `verified_current`, `pending`, or `error`;
- `richArchiveCompletenessStatus`: `full_rebuild_required`,
  `incremental_pending`, `mutable_refresh_pending`, or `mutable_refresh_stale`;
- `richArchiveCurrentGenerationId` and `richArchiveCurrentGenerationSha256`;
- `richArchiveInventoryDigest` and `richArchiveVerifiedCutoff`;
- `richArchiveMutableScanCursor`, cycle ID, and scan timestamp;
- `richArchiveLastErrorReason`: one fixed typed error enum or null.

Incremental writes always make full completeness stale/pending.  They never set
`richArchiveStatus=PASS`, `caught_up`, or a 100% completeness value.

Queue reasons are fixed strings:

- `rich_full_rebuild_required`
- `rich_archive_repair_required`
- `rich_incremental_partial`
- `rich_incremental_read_error`
- `rich_incremental_merge_error`
- `rich_incremental_readback_error`
- `rich_mutable_refresh_pending`

Queue is persisted before state.  If the process crashes after publishing
`CURRENT` but before state replacement, replay is idempotent and the old cursor
remains safe.  If it crashes after queue persistence, retry ownership survives.

## Typed error contract

Public output and managed receipts contain counts and one of the reviewed error
categories only; never message bodies, tokens, URLs, or private paths.  At minimum
the enum distinguishes input/schema, inventory, lock, Discord authentication,
Discord response, rate limit, rich-core contract, baseline, merge, readback,
queue persistence, state persistence, and journal recovery failures.

## Crash and recovery invariants

- no cursor is newer than the selected canonical generation;
- a published generation is never rolled back because state persistence failed;
- an unpublished or interrupted journal is recovered under the same owned lock;
- retry queue ownership is durable before a failing entry can be reported;
- crash at each CURRENT/queue/state boundary leaves either a safe replay or an
  explicit repair/full-rebuild queue item;
- the asset budget is never recreated inside the per-entry loop.

## Acceptance tests

- candidate ordering, exclusions, active-queue ownership, null cursor, and quiet
  baseline behavior;
- page/message/write/entry/response/429/asset caps and exactly-full pagination;
- one module-issued token and one budget object across multiple entries;
- forged/wrong/closed token rejection and unsupported core contract rejection;
- exact fetched ID and API-source-hash comparison against CURRENT;
- wrong merge schema, omitted ID, stale CURRENT, wrong generation hash, and entry
  identity mismatch all fail before cursor advancement;
- rotating mutable scan progression, wrap, partial cycle, and crash replay;
- crash injection before/after CURRENT, queue, and state plus journal recovery;
- typed errors, redacted output, deterministic command topology, and no prompt
  fallback;
- source/package byte parity, full pytest, compile, secret scan, and independent
  reviewer PASS before integration.

## Completion and deployment gate

This branch is complete only when the core contract is independently approved,
the final adapter passes every acceptance test, source and packaged Skill are
byte-identical, the full suite passes, and the worktree is committed and clean.
Live deployment remains a separate transactional Human Gate with exact cron
readback and rollback evidence.
