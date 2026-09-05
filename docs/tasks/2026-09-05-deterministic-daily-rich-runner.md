# Deterministic Daily Rich Archive Runner

Status: approved for implementation as part of Jasper's 2026-09-05 direct
repair request and the approved rich archive specification.

## Background

The three 05:30, 05:40, and 05:50 daily-sync jobs are currently `agentTurn`
jobs. Their prompt explicitly permits an LLM fallback that can append Markdown
and advance state without using the canonical rich-message archive. That path
can report success while component-only, embed-only, or other rich Discord
payloads are lost.

This task removes the prompt execution path. All three slots become fixed-argv
command jobs that invoke one deterministic runner and one archive API.

## Scope

In scope:

- preserve the three roles, names, schedules, timeouts, failure alerts, and
  `delivery=none` behavior;
- render all three jobs as isolated fixed-argv command jobs;
- serialize them, backlog, reconcile, and rebuild writers with the existing
  state-parent `.channel_backup.lock` file;
- add `run_daily_sync_v3.py` and managed-runner support for all three roles;
- require `RichArchiveStore.merge_messages()` and fail closed when the module or
  verified API contract is absent;
- remove the daily prompt fallback from the package;
- retain exact adoption and rollback support for legacy `agentTurn` daily jobs;
- update tests, documentation, the OWASP record, and the deterministic `.skill`
  artifact.

Out of scope:

- implementing the rich normalizer, renderer, downloader, or full-history
  rebuild itself;
- modifying live cron jobs or installed Skills;
- deleting legacy raw files or duplicate records;
- changing backlog, reconcile, or weekly runner internals on this branch.

## Runtime contract

`run_daily_sync_v3.py` performs one bounded slot:

1. Resolve state, queue, inventory, backup root, and OpenClaw config through
   reviewed fixed argv. Reject missing, non-object, or path-escaping inputs.
2. Acquire `state.parent / ".channel_backup.lock"` non-blocking and keep it for
   the complete inventory/read/archive/state transaction. Lock contention is a
   structured data-preserving skip.
3. While holding the lock, require today's deterministic inventory to have
   `ok=true`, `remainingMissing=0`, and no warnings. An incomplete inventory is
   a warning skip before Discord reads or archive writes.
4. Reload current state and queue. Select at most six entries in stable order.
   Eligible entries have a non-null durable cursor, are healthy, are within the
   two-day daily freshness window, are not excluded, and have no active queue
   item. Null-cursor, stale, partial, queued, retry, and error entries remain
   owned by backlog.
5. Read at most 30 messages per page, 60 per entry, 180 for the slot, and write
   at most four entries. A second page is allowed only when the first page has
   exactly 30 messages.
6. For a non-empty entry, instantiate
   `RichArchiveStore(entry_root, lock_path)` and call:

   `merge_messages(messages, channel_id=..., observed_at=..., generation_id=...,
   lock_already_held=True)`.

   The call must return a mapping with a non-empty `generationId` and a positive
   verification result. `resolve_current()` must select that same generation.
   Missing methods, mismatched generations, incomplete verification, channel
   mismatch, attachment failure, or any exception is fatal for the slot.
7. Only after the archive generation is verified may the runner advance
   `lastWrittenMessageId` and its compatibility alias `lastMessageId` to the
   highest message ID actually committed. State and queue use atomic replace.
8. An entry that reaches a page/message/read cap is marked `partial` and
   upserted in the queue with `page_limit_reached`. Otherwise it is marked
   healthy and receives today's `lastBackup`.
9. Same-ID refreshes belong to bounded lookback/weekly refresh and must not
   advance the new-message cursor.

The deterministic runner never writes `raw/*.md`, canonical JSONL, or attachment
bytes itself. It never shells out, evaluates Discord content, or sends messages.

## Managed receipt contract

The command prints one bounded JSON object. `run_managed_component.py` writes the
existing `daily-sync-v1` producer receipt for the exact slot role:

- lock contention: `warning`, `backup_lock_busy`, exit success;
- inventory gate: `warning`, no read/write, exit success;
- successful checked/written/queued counts: `ok` or `pending` when queued work
  remains;
- missing/unverified rich archive API, Discord error, store error, or state
  persistence failure: `error`, non-zero exit.

Message bodies, tokens, message IDs, paths, and raw exception text are excluded
from public stdout and component receipts. Detailed subprocess output stays in
the existing private bounded log.

## Cron compatibility

- Manifest contract remains eleven owned jobs and `contractVersion=v1`, so
  declaration keys stay stable for an in-place transactional update.
- Daily jobs change only from `agentTurn` to `command`; their session target is
  `isolated` because serialization is enforced by the shared file lock.
- The legacy adoption allowlist accepts exact old `agentTurn` or command daily
  jobs only when ID, fingerprint, schedule, timezone, and role token all match.
- Rollback receipts retain the complete original `agentTurn` contract, including
  its agent and tools policy, so an interrupted upgrade can restore it exactly.
- The activation canary proves two daily lock users cannot overlap; no model or
  persistent-session canary is required.

## Acceptance tests

- all three manifest rows are command jobs with unchanged schedules and fixed
  `run_managed_component.py --role daily-sync-N` argv;
- no shipped prompt or manifest permits direct/fallback raw Markdown writes;
- missing rich module and unverified/mismatched generation fail before cursor
  movement;
- lock contention and incomplete inventory produce warning receipts without
  Discord reads;
- deterministic selection excludes active queue, stale, null-cursor, partial,
  retry, error, and excluded entries;
- 30/60/4/6/180 limits and `page_limit_reached` queue handoff are tested;
- archive commit precedes cursor persistence; injected store/state failures
  preserve the old cursor;
- exact legacy daily adoption and rollback still pass;
- topology canary validates file-lock serialization;
- targeted tests, complete pytest suite, package parity, compile, secret scan,
  and OWASP A01-A10 evidence pass.

## Stop conditions

Do not deploy live, push, delete legacy data, or weaken a verification gate on
this branch. Stop and report if the rich archive API cannot satisfy the verified
generation contract or if compatibility would require changing declaration
keys or schedules.
