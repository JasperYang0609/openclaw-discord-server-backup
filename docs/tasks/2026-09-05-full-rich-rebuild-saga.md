# Full Rich Discord Archive Rebuild Saga

Date: 2026-09-05

Status: implementation-ready task contract; live cutover remains `BLOCKED` until every gate in this document and the companion security plan passes.

Authorization: Jasper requested direct repair. That authorization covers a non-destructive rebuild and transactional local cutover. It does not authorize deleting legacy evidence, weakening a completeness gate, changing Discord permissions, pushing a release, or deploying an unreviewed package.

## Outcome

Rebuild the complete managed Discord archive as rich, deterministic, locally verifiable generations without advancing the existing incremental cursors. The run must be resumable, must never expose a mixed 178-entry generation, and may select a new archive only after live evidence proves all selected entries complete through their recorded cutoffs.

The current accepted entry baseline is exactly 178 stable Discord channel/thread identities. The implementation must not hard-code 178 as a permanent product limit. For this repair, any fresh inventory with fewer or more identities blocks the run and requires a separately reviewed baseline update; it must never silently trim a new thread or ignore a disappeared entry to preserve a `178/178` result.

## Deliverables

- A deterministic full-history coordinator and resumable run journal.
- One immutable rich generation for each of the 178 expected entries.
- One immutable run manifest binding all entry identities, cutoffs, generation hashes, evidence hashes, and coverage counts.
- One archive-root `RUN_CURRENT.json` pointer replaced atomically only after the entire run passes.
- Private, checksummed runtime and offline-audit receipts.
- An immutable pre-repair baseline and a `legacy-retained` projection; no destructive cleanup.
- Tests and evidence required by `docs/security/2026-09-05-full-rich-rebuild-saga-owasp.md`.

## Non-goals

- Do not delete, rewrite in place, or deduplicate the legacy Markdown archive.
- Do not claim recovery of Discord messages deleted before the scan and absent from local evidence.
- Do not claim a complete historical timeline of edits, reactions, pins, poll counts, or signed URLs when Discord exposes only the current observation.
- Do not post progress messages from the rebuild runner, mutate Discord content, change guild permissions, or enable privileged intents.
- Do not advance `lastWrittenMessageId`, queue cursors, `lastBackup`, or any other state/queue field during enrichment.
- Do not publish, push, install, or change live cron from this specification task.

## Fixed inputs and root identity

The coordinator requires explicit, prevalidated inputs. It must not search the filesystem for a likely archive or select the newest-looking baseline.

- `expectedArchiveRoot`: the absolute `backupRoot` from the canonical installed configuration.
- `expectedStatePath` and `expectedQueuePath`: the canonical installed files.
- `expectedBaselineDir` and `expectedBaselineSha256`: the immutable pre-repair evidence selected for this repair.
- `expectedEntryCount=178` and `expectedEntrySetSha256`: the approved stable-ID/path baseline.
- `guildId`, configured timezone, Discord adapter identity/hash, and bounded policy values.
- `reportChannelId`, if operational reporting shares an archived entry.

Before any directory creation or network read, fail closed unless:

- `config.backupRoot`, `state.rootPath`, and `expectedArchiveRoot` are the same lexical and resolved real directory;
- the archive root, workspace, receipt root, baseline root, and staging root are pairwise non-overlapping where required;
- every original path component is a real non-symlink directory with expected owner/mode/link count;
- the root device/inode, configuration digest, state digest, queue digest, and baseline digest match the supplied expectations;
- the baseline verifier independently recomputes its manifest and returns `PASS`;
- the 178 expected entries have unique channel IDs and unique NFKC/case-folded relative paths.

The public task record stores logical field names and hashes. Exact local paths belong only in private execution receipts.

## Discord inventory contract

The inventory collector must use stable IDs and exhaust every applicable REST endpoint, including:

- all visible guild text-capable channels and thread-capable parent channels;
- guild active threads;
- every page of archived public threads for every applicable parent;
- every page of archived private threads available to the bot for every applicable parent;
- every page of joined private archived threads for `@me` for every applicable parent.

For each endpoint, evidence records request identity, page cursor, response ID set/hash, response count, retry observations, and an explicit terminal page. A list that is truncated, warning-bearing, capped before a terminal page, or affected by an unresolved 401/403/404/429/5xx is incomplete.

The fresh inventory must bind:

- guild ID, channel/thread ID, type, parent ID, archived/active state, and stable relative path;
- public/private/joined-private enumeration class;
- all terminal-page proofs and a canonical inventory digest;
- the exact 178-entry eligible set from state and the approved baseline.

An active or archived thread discovered outside the expected set blocks this repair run. A missing expected entry also blocks. Explicit terminal exclusions are permitted only if they were already part of the reviewed baseline and are not counted in the 178 selected entries.

## Permission and content-capability preflight

The collector must prove, rather than assume, that the bot can read every selected entry.

- Guild membership and application identity match the configured guild.
- `VIEW_CHANNEL` and `READ_MESSAGE_HISTORY` are effective for every selected channel/thread.
- Private threads are accessible or joined as required by the approved inventory contract.
- The `MESSAGE_CONTENT` privileged intent/capability is enabled and effective.
- Each inventory and message endpoint completes without unresolved permission warnings.

Blank `content` is not proof that a message is truly empty. Capability evidence must combine application/intent readback where available with one-shot reads of approved, already-existing non-empty canary messages across the distinct permission classes. The canary IDs and expected stable visible fingerprints come from immutable evidence; the run must not create or edit a Discord canary. A mismatch, redacted payload, or unavailable permission class is `BLOCKED`, not a zero-content success.

## `FullRebuildRunContext`

Exactly one coordinator-created `FullRebuildRunContext` owns all mutable run authority. Entry workers receive non-serializable child views and cannot create or reset budgets, lock authority, or runtime evidence capabilities.

The context binds:

- schema/contract version, random run ID, process identity, start/deadline, and configured timezone;
- expected archive-root device/inode, baseline identity/digest, guild ID, 178-entry set digest, and fresh inventory digest;
- state/queue byte hashes that must remain unchanged;
- a module-issued shared-lock ownership token for the current lease epoch;
- a global REST request/retry/runtime budget;
- a global asset file-count, declared-byte, downloaded-byte, redirect, and probe budget shared across all 178 entries;
- per-file, per-message, per-entry, and full-run limits;
- a non-sparse disk reservation identity, requested bytes, allocated blocks, consumed bytes, released bytes, and minimum free-space floor;
- one-shot runtime evidence registrations and expiry;
- durable run-journal path, root receipt path, and selected run-manifest path.

### Global disk reservation

Before downloading any asset, calculate the full-run upper bound from declared Discord sizes, configured caps for unknown sizes, staging/canonical/Markdown estimates, largest-file temporary-copy headroom, journal/manifest overhead, and a safety margin. Require:

`freeBytes >= requiredReservationBytes + configuredFreeSpaceFloor`.

Reserve the bytes with a private, regular, single-link, non-sparse file. On macOS, allocation must use a real preallocation mechanism and verify allocated blocks; sparse `truncate` alone is not a reservation. If real reservation cannot be established, fail before the first asset mutation. Release reserved blocks only as verified immutable assets consume equivalent space, and recompute actual usage on resume. The context serializes all asset writes so entry workers cannot overcommit the global budget.

## Runtime evidence capability

Persisted JSON is audit evidence, never authority to assert live completeness. Only the bounded Discord collector may mint a non-serializable, in-process, one-shot runtime evidence capability.

Each capability is bound to:

- run ID, lease epoch, inventory digest, entry identity, cutoff, baseline digest, adapter code hash, and exact evidence hash;
- issuer PID/process start identity, cryptographically random nonce digest, issued monotonic time, and a short configurable TTL no greater than five minutes;
- a single operation (`install-entry-evidence`, `verify-entry`, or `commit-run`) and exactly one consumption.

Capabilities cannot be constructed from CLI input or deserialized from disk. They expire on deadline, process change, fork, lock release, lease-epoch change, first use, or close. Receipts store only the capability ID/digest, issue/consume timestamps, and binding fields; never the nonce or Discord credential. Offline verification may return `AUDIT_VERIFIED`, but only a current runtime capability can produce a transient `PASS` used by the saga.

## Per-entry full-history generation

For each selected entry, the coordinator performs a bounded backward scan under an entry-specific high watermark.

- Read `limit=1` to capture the cutoff and preserve the sanitized response plus hash.
- Enumerate backward with `before`, maximum page size 100, through an explicit terminal page.
- Reject repeated IDs, repeated pages, non-moving cursors, IDs outside the cutoff, oversized responses, malformed bodies, and page-count/message-count bounds.
- Normalize every message through the reviewed rich normalizer.
- Render deterministic Markdown from canonical records only.
- Inventory and locally preserve every in-scope Discord-hosted binary asset; metadata-only external media remains explicit and outside the binary denominator.
- Merge same-ID current observations and retained local revisions according to the rich archive contract.
- Write and fsync a private immutable staged generation and its entry receipt.

### Explicit empty-entry proof

An empty entry passes only when all of these are true in the same fresh evidence window:

- the cutoff request returned an explicit empty response;
- the first history page returned an explicit empty terminal response;
- both responses are successful, untruncated, bounded, and cryptographically bound to the entry;
- inventory and permission/capability preflight include that exact entry;
- no baseline/local live-ID evidence contradicts emptiness.

`null`, missing pages, 403/404, inaccessible private history, or an adapter default may never become a true-empty proof.

## Per-entry atomicity and run-level selection

Each entry generation is immutable after verification. A staged entry may be resumed and then sealed, but may not be modified after its generation hash is recorded.

The archive root also owns a run namespace:

```text
runs/<run-id>/
  run-journal.json
  run-manifest.json
  receipts/full-rebuild-run.json
  entries/<entry-identity>.json
RUN_CURRENT.json
```

`run-manifest.json` contains the exact selected generation and receipt hash for every expected entry. Readers and the indexer resolve `RUN_CURRENT.json` first and then use only the 178 generations listed by that manifest. They must not enumerate arbitrary `generations/` directories or combine independently observed entry pointers.

Compatibility per-entry `CURRENT.json` pointers may be updated after the root commit, but they are not the authoritative full-run selector. Their updates are journaled and verified against the selected root manifest. A crash before root-pointer replacement exposes the old run; a crash after replacement exposes the complete new run. No crash point may expose a mixed generation as the selected full archive.

## Resumable saga

The durable state machine is monotonic:

- `PREPARED`: paths, configuration, baseline, budgets, and quiescence plan validated without mutation.
- `INVENTORY_VERIFIED`: fresh complete inventory and permissions prove exact 178-entry scope.
- `BASELINE_FROZEN`: pre-repair archive/state/queue evidence verified immutable.
- `REBUILDING`: entry stages are built and sealed independently.
- `BASELINE_COMPLETE`: all 178 baseline cutoffs are locally complete.
- `DELTA_CONVERGING`: post-baseline new IDs and bounded same-ID mutable observations are merged.
- `VERIFYING`: run manifest and all four coverage dimensions are recomputed independently.
- `READY_TO_COMMIT`: transient runtime PASS exists for every entry and the root binding.
- `COMMITTED`: `RUN_CURRENT.json` replacement, directory fsync, and exact readback succeeded.
- `PAUSED` or `FAILED`: no new root selection; reason and safe resume point are durable.

Every transition is receipt-first, atomic, fsynced, and idempotent. Startup recovery verifies the last durable phase and all referenced bytes before continuing. It never trusts a phase name without recomputing the phase preconditions.

## Maintenance, pause, and lock reacquisition

The shared backup lock is mandatory for every filesystem mutation and for root commit. The coordinator must quiesce the managed backup writers before the first mutation.

A long run may release the lock only at a durable maintenance checkpoint where:

- all open temporary files are closed;
- staged content and the journal are fsynced;
- no pointer or state/queue mutation is in progress;
- the current lease token is closed and cannot be reused;
- the receipt records why the lock was released and exact pre-release hashes.

On resume, a newly minted lock token creates a new `leaseEpoch`. Before any write, the coordinator rechecks process ownership, archive-root device/inode, config, state/queue byte hashes, baseline, run journal, all sealed generations, free space/reservation, cron quiescence, and a fresh inventory. It records a `reacquireEvidenceSha256` binding old/new epochs and the revalidated bytes. Any unexplained drift fails closed. A safe Discord-only drift proceeds through the delta-convergence phase; state/queue or selected-generation drift requires operator review and must not be auto-merged.

## Baseline, delta, and convergence rounds

The run does not pretend Discord stops changing during a long rebuild.

- Baseline round: scan every entry through its independently captured baseline cutoff.
- Delta round: fetch all new IDs after each baseline cutoff, refresh the bounded mutable lookback, and seal successor entry generations.
- Zero rounds: require two consecutive complete guild-wide rounds with zero non-self-drift new IDs, zero changed same-ID visible/source fingerprints inside the refresh scope, zero unresolved asset refreshes, and terminal-page evidence for all 178 entries.

The second zero round must use fresh inventory and fresh runtime capabilities; it cannot reuse the first round's receipts. Any ordinary user/bot message or mutable observation resets the zero-round count.

### Report-channel self-drift suppression

The runner itself is silent until the root transaction reaches a terminal state. If surrounding orchestration must post status into an archived report channel, suppression is allowed only for an explicit list of Discord message IDs returned by those exact sends and bound to this run ID in a private receipt. Author-name, content-pattern, timestamp, or channel-only heuristics are forbidden.

Explicitly listed self-report IDs newer than the report entry cutoff:

- are not used to keep the two zero-round loop alive indefinitely;
- are recorded as `selfDriftDeferred`, with IDs and source receipt hash;
- are never counted as archived in the committed run;
- are queued for the first deterministic incremental run after commit.

If exact outbound IDs are unavailable, suppression is disabled and convergence waits. User messages and unrelated bot messages can never be suppressed.

## REST retry and signed-URL handling

- Use fixed HTTPS Discord API/CDN hosts, fixed argv/network adapters, no environment proxy, no netrc/cookies, and no Bot Authorization header on asset requests.
- Honor a valid Discord 429 `retry_after` within the global deadline and retry budget. Apply bounded exponential backoff with jitter to retryable 5xx/transport failures.
- Treat 400/401/403/404, malformed JSON, truncated/oversized bodies, inconsistent rate-limit metadata, retry exhaustion, and deadline exhaustion as explicit run errors.
- Bound requests, response bytes, pages, redirects, runtime, and total sleep across the full run, not per entry.
- Revalidate scheme, host, port, userinfo, DNS/IP class, and redirect target at every asset hop.
- Prefer a Discord safe proxy URL when present. Retain original/proxy metadata separately.
- If a Discord signed URL expires, perform a bounded REST refresh of the exact parent message, require identical channel/message/asset identity, record a new signed-URL observation, and retry within the global quota. Signature/query expiry changes do not create a content revision or alter the stable asset identity.
- A missing/expired asset that cannot be refreshed remains an attachment error and blocks entry/run PASS.

## Same-ID mutable truth boundary

The run proves the current API-visible state through each recorded observation window. It does not prove changes Discord no longer exposes.

- Current content, components, embeds, polls, stickers, snapshots, reference context, flags, pin state, reactions, asset metadata, and edit timestamps are preserved as observed.
- Existing local content revisions and observations are retained with provenance and are never silently overwritten.
- Conflicting equal-timestamp content revisions fail closed; hash ordering is not a substitute for temporal authority.
- Reactions, pin state, poll counts, embeds, and URL observations are mutable observations even when `edited_timestamp` is unchanged.
- Deleted messages, removed attachments, and pre-run edit/reaction history absent from both Discord and immutable local evidence are reported as historical unknowns, not counted as reconstructed truth.

The root receipt must label the guarantee `current-state-complete-through-cutoff`, not `complete-edit-history`.

## Legacy retention

- Preserve the original archive and immutable pre-repair evidence byte-for-byte.
- Preserve local-only, Discord-deleted, duplicate, zero-byte, and parser-unknown legacy artifacts under a checksummed `legacy-retained` projection with source path/hash and classification.
- Never count retained-only records in the live ID/visible/binary denominator.
- Never delete or overwrite legacy data during rebuild, rollback, resume, or post-commit cleanup.
- Any later destructive compaction requires separate authorization and a verified restore canary.

## Root receipt schema and binding

The private root receipt uses schema `openclaw-discord-full-rich-rebuild-run.v1`. Stored status is `AUDIT_ONLY`, `PAUSED`, or `FAILED`; a stored file may not self-assert runtime `PASS`.

Required fields:

- contract/schema version, run ID, phase, created/updated/committed timestamps;
- logical archive-root identity plus device/inode/config digest;
- guild ID, expected entry count `178`, expected entry-set digest;
- baseline snapshot ID and state/queue/archive manifest hashes;
- adapter/package/source hashes and permission/MESSAGE_CONTENT evidence hash;
- inventory endpoint classes, counts, terminal proofs, digest, and freshness window;
- global request/retry/runtime/asset budgets and disk reservation accounting;
- maintenance checkpoints and lock-reacquisition evidence for every lease epoch;
- 178 entry bindings: stable identity, cutoff, true-empty proof when applicable, generation hash, entry-receipt hash, live-evidence hash, canonical/message/visible-pointer/Markdown/asset counts, error counts, and transient capability-consumption record;
- baseline, delta, and both zero-round digests, including exact self-drift deferred IDs;
- legacy-retained manifest hash and immutable-evidence re-verification result;
- state and queue before/after byte hashes, which must be identical;
- root run-manifest hash, prior/new `RUN_CURRENT.json` bytes/hashes, fsync/readback evidence;
- aggregate gate counts, error enums, package/test/security evidence references, and reviewer decision.

`runManifestSha256` binds the sorted entry-binding array. `fullRunBindingSha256` binds the archive identity, baseline, inventory, permission evidence, all entry bindings, convergence rounds, legacy manifest, state/queue invariants, and run manifest. Coverage percentages are derived output; the verifier trusts exact expected/verified sets and counts, not caller-supplied percentages.

## Completion gate

The run may replace `RUN_CURRENT.json` only when a single runtime verification proves:

- fresh inventory selected exactly 178 of 178 expected entries;
- every inventory endpoint/class completed through a terminal page;
- permission and `MESSAGE_CONTENT` evidence passed;
- live errors, pagination errors, unknown visible fields, and attachment errors are all zero;
- live IDs equal canonical IDs for every entry and globally;
- canonical duplicate IDs are zero within each entry;
- independently censused visible-pointer coverage is 100%;
- deterministic Markdown block/hash coverage is 100%;
- recursive in-scope Discord binary coverage is 100%;
- all true-empty entries have explicit terminal proof;
- every entry has a verified cutoff, immutable generation hash, live-evidence hash, and consumed one-shot runtime capability;
- two fresh consecutive zero rounds passed;
- legacy evidence re-verification passed;
- state and queue bytes are unchanged;
- fault-injection, targeted/full tests, package parity, secret scan, and OWASP review passed;
- no open P0 or P1 remains and no unaccepted completeness/security P2 remains.

After pointer replacement, exact readback and an isolated reader/indexer canary must resolve the new run manifest and all 178 selected generations. Failure triggers checked pointer rollback to the prior exact bytes while retaining the failed run for diagnosis.

## Required test matrix

### Inventory and permissions

- Exact 178/178 success and 177/178, 179/178, duplicate ID, Unicode/case-fold path collision failures.
- Active threads plus every archived public/private/joined-private page; partial terminal enumeration and permission failures.
- Missing `VIEW_CHANNEL`, `READ_MESSAGE_HISTORY`, private-thread membership, and effective `MESSAGE_CONTENT`.
- Truly empty entry versus redacted/403/null/missing response.

### Pagination and convergence

- Cutoff capture, boundary inclusion, repeated/stalled page, duplicate ID, wrong-channel ID, oversized/truncated response, max-page/message bound.
- Messages arriving during baseline, delta capture, two zero rounds, and reset on new user message or same-ID mutation.
- Report self-drift only by exact returned outbound IDs; reject author/content heuristics.
- 429 retry-after, bounded 5xx retry/jitter, retry exhaustion, timeout, cancellation, and global deadline.

### Content, assets, and truth limits

- All rich payload fixtures and independent visible-pointer mutation tests from the rich archive specification.
- Signed-URL expiry and exact-message refresh; changed asset identity fails.
- Global request/asset quotas cannot reset per entry or resume.
- Real disk reservation, insufficient capacity, concurrent external disk pressure, crash during reservation release, and resume accounting.
- Same-ID equal-timestamp conflict, mutable observation change without edit timestamp, retained prior revisions, deleted/live-missing classification.

### Saga, crash, and rollback

- Fault injection before/after every journal write, file/directory fsync, entry seal, run-manifest write, compatibility pointer update, root-pointer rename, and readback.
- Kill/restart in every saga phase; only the old or complete new root run is selectable.
- Maintenance pause, lock release, stale-token rejection, lock reacquisition, state/queue drift, archive-root inode change, generation tamper, and cron de-quiescence.
- Resume skips only independently verified sealed entries and rechecks every referenced hash.
- Cursor/state/queue bytes remain exact before, during, after success, and after failure/rollback.
- Root readback and reader/indexer restore canary across all 178 bindings.

### Security and packaging

- OWASP A01-A10 cases in the companion plan.
- Source/dist exact parity, deterministic package, Python compile, full test suite, dependency/secret scan, diff check, and fresh independent review.

## Stop conditions

Stop without root cutover and report `BLOCKED` if any required entry, endpoint, permission, evidence hash, asset, budget, disk reservation, terminal proof, coverage dimension, zero round, state/queue invariant, package parity, or security gate is unknown or failing. Preserve all staged generations and receipts for a safe resume; do not lower the gate or substitute an old inventory.

## Implementation sequence

- Implement the coordinator, context, durable saga, root selector, and root verifier behind tests.
- Integrate the reviewed rich archive core and deterministic daily writer adapter.
- Build and verify package parity; obtain fresh independent review.
- Snapshot live inputs and quiesce managed writers transactionally.
- Run baseline, delta, and two zero rounds; do not advance cursors.
- Execute runtime root gate and atomic `RUN_CURRENT.json` cutover.
- Run exact readback, reader/indexer canary, and deterministic incremental capture for explicitly deferred self-report IDs.
- Record private closeout evidence. Only then may the parent deployment workflow consider live completion.
