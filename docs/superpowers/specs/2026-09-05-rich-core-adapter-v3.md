# Rich Core Adapter V3

Status: approved for isolated implementation by the 2026-09-05 direct-repair
request. Live deployment, full-history rebuild, and full-rebuild saga APIs are
outside this branch.

## Objective

Replace the daily runner's caller-supplied adapter and self-attested Mapping
receipts with one integrity-checked production adapter. Only opaque,
module-issued runtime capabilities may authorize a state or cursor update.

## Runtime loading

`run_daily_sync_v3.py` loads only the sibling `rich_core_adapter_v3.py`. A
versioned runtime manifest binds that adapter and `rich_message_archive.py` by
SHA-256, owner, exact file mode, regular-file type, and single-link identity.
Symlinks, hardlinks, unsupported contracts, path substitution, and permissive
fallback imports fail before the shared lock or mutable state is opened.

Production has no adapter/factory command-line option and no caller-supplied
factory parameter. Test seams may replace the managed loader only by
monkeypatching process-local code; they are not reachable from the CLI.

## Canonical lock and session

The only accepted shared lock is `<archive-root>/.channel_backup.lock`. A
different caller path fails before mutation. This intentionally differs from
the legacy state-parent lock; deployment must quiesce legacy jobs before the
new topology is enabled.

The adapter opens one locked slot, then mints one rich incremental run context
covering the exact selected inventory. A session is bound to PID, process-start
nonce, session nonce, lease epoch, canonical lock file identity, archive root,
entry identity, inventory digest, and the run-wide asset budget.

## Capability chain

The supported authority chain is:

1. `inspect_current(entry)` returns `CurrentReadbackCapability`.
2. `merge_incremental(request, pre_current=capability)` consumes the current
   capability, performs the core merge, re-reads CURRENT, and returns
   `IncrementalCommitCapability`.
3. `authorize_state_update(commit, requested_cursor=...)` consumes the commit,
   rechecks CURRENT, and returns `StateUpdateGrant`.
4. The runner consumes the grant once and uses only its core-derived update
   fields.

For an audited empty baseline, `probe_head(entry, fetch_page=...)` calls the
bounded transport inside the trusted session. Only a module-issued explicit
empty probe plus the matching current capability can authorize a null-cursor
quiet update. A non-empty head probe queues a full rebuild and cannot advance a
cursor.

Capabilities cannot be constructed outside the module, copied, deep-copied,
pickled, serialized, or replayed. They expire by TTL and become invalid after
single use, session close, lock release, fork/PID change, root/entry/inventory
substitution, or CURRENT drift. Persisted receipts are `AUDIT_ONLY` and never
restore runtime authority.

## Cursor rules

- New-message cursor advancement is limited to the greatest new message ID
  durably present in the committed generation.
- Mutable-only merges keep the cursor unchanged.
- Partial pagination may advance only to the greatest durably committed new ID
  and must enqueue remaining work.
- A caller-proposed cursor that is non-canonical, absent from committed data,
  older/newer than the core-derived safe value, or unrelated to the session is
  rejected.
- Null cursor is authorized only for an audited-empty generation and an
  explicit-empty head probe from the same session.

## Failure model

Public failures use fixed redacted categories. Integrity, contract, authority,
baseline, merge, readback, cursor, and budget failures are distinct. Queue is
persisted before state. A published generation with an unpersisted state update
is replay-safe; cursor never leads CURRENT.

Full-rebuild entry and root APIs are declared only as fail-closed stubs in this
branch. A separate reviewed saga owns seal-without-CURRENT and root
`RUN_CURRENT.json` publication.

## Acceptance

- Forged commit/current Mapping pairs cannot authorize state.
- Capability construction, copy, pickle, replay, cross-session/root/entry/PID,
  expiration, lock substitution, and CURRENT drift are rejected.
- The canonical lock is exact and acquired before mutable state/queue reads.
- Incremental, mutable-only, partial, quiet, and null-baseline cursor rules pass.
- Managed loader rejects SHA, owner, mode, link, contract, and path drift.
- Source, packaged Skill, and runtime manifest have exact parity.
- Targeted and full pytest, compile, diff, secret-shape scan, and OWASP gate pass.

