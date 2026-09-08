# Daily Rich Runner Bootstrap Gate Repair

Status: code-complete locally; live deployment is blocked until the full rich
archive baseline is built and verified.

## Incident

The first natural 05:30, 05:40, and 05:50 daily-sync window failed with the
public category `rich_archive_merge_failed`. The live state and queue cursors
did not move, so no message was falsely claimed as durable.

Two independent deployment gaps were confirmed:

- the installed Skill did not contain `run_daily_sync_v3.py`, so the agentTurn
  job discovered and invoked a development-worktree copy;
- the archive entries did not contain a verified rich `CURRENT.json` baseline,
  while the deterministic runner is incremental-only by contract.

The underlying rich store condition was therefore "CURRENT generation is
missing; full rebuild required", but the runner collapsed that condition into
the generic merge category.

## Repair

- Package the deterministic runner, rich archive core, managed-component
  adapter, fixed-argv command topology, and shared lock canary in the canonical
  Skill and deterministic `.skill` artifact.
- Require every selected entry to resolve a valid current rich generation
  before the first Discord message read.
- Report a missing baseline as `rich_archive_not_initialized` and an invalid
  pointer as `rich_archive_current_invalid`.
- Retain only the exception class in the private bounded component log; never
  emit exception text, paths, tokens, message IDs, or message bodies.
- Preserve state and queue bytes when the baseline gate fails.
- Remove the obsolete persistent-session canary code after daily sync moves to
  isolated command jobs serialized by the shared filesystem lock.

## Evidence

- Targeted runner/topology/managed/package tests: 77 PASS.
- Complete repository suite: 293 PASS.
- Repository `post_run_check.py`: PASS.
- Python compile, `git diff --check`, package/source parity, and secret-signature
  scan: PASS.
- Live read-only preflight: exit 2 with
  `rich_archive_not_initialized`; state and queue SHA-256 values unchanged.

## Remaining gate

Do not deploy the command topology yet. First integrate and execute the reviewed
full rich rebuild coordinator against the approved immutable baseline, verify a
valid `CURRENT.json` for every eligible entry, then run one isolated installed
runner smoke. Only after those gates pass may the live topology be transactionally
upgraded and the next natural schedule window used for final acceptance.

## Interrupted materialization recovery

The first live full baseline stopped after the third entry had completely
materialized roughly 1.2 GB of canonical, raw, and attachment bytes but before
an asset reservation, PASS receipt, manifest, or generation seal existed.
Safe recovery now permits that exact unsealed stage to be reused only when:

- its only receipt is the checksummed stage-base CURRENT receipt;
- every canonical/raw/attachment file is a regular single-link file and the
  staged file set contains no unexplained files;
- every attachment byte length and SHA-256 still matches its canonical receipt;
- a fresh bounded Discord enumeration has the same message set, timestamps,
  visible/source fingerprints, renderer accounting, and asset identities;
- the only accepted source difference is a verified Discord CDN signature
  query change whose stable URL identity is unchanged.

The stage is rewritten to the fresh source records, revalidated locally, then
continues through the existing reservation, PASS-evidence, manifest, and seal
gates. Any filename, stable URL, size, content, message, day partition,
unexpected receipt, manifest, symlink, hardlink, or hash change rejects reuse
without creating a reservation or PASS receipt.

Candidate evidence: 445/445 tests PASS; positive no-redownload resume,
semantic-asset-change rejection, and tampered-byte rejection PASS; deterministic
package/source parity and repository post-run check PASS. Live baseline and
natural-schedule acceptance remain pending.

## Long-running baseline probe-budget repair

The resumed live baseline exposed a separate fail-closed false positive after
five entries: the 60-second unknown-size metadata budget was implemented as an
absolute deadline beginning when the full run started. Message enumeration,
attachment download, hashing, and generation sealing therefore consumed the
deadline even though they issued no metadata probes.

The budget now charges only elapsed time inside an actual credential-free CDN
HEAD probe. The shared request-count cap, cumulative active-probe time cap,
single-request timeout, exact Discord CDN host allowlist, global-address
resolution, redirect restrictions, content-length validation, and all file,
entry, run, and disk-capacity quotas remain unchanged. Non-probe work and idle
time cannot exhaust the metadata budget, while cumulative active probe time
still fails closed once its configured cap is exceeded.

Candidate evidence: 447/447 tests PASS, including a long non-probe wall-time
gap regression and a cumulative active-probe elapsed-cap rejection. Live
baseline continuation and final installed/natural-schedule acceptance remain
pending.

## Live embed schema and interrupted evidence prefix

The sixth live entry then failed closed because Discord returned two preserved
embed leaves that the independent census had not yet classified:
`embed.reference_id` and media `description`. Both leaves were already retained
and included in deterministic structured Markdown; the schema census now
classifies them explicitly while continuing to reject any other unknown nested
embed field.

The same interruption occurred after attachment reservation and persisted live
evidence but before an audit receipt or manifest. Resume now accepts only that
exact incomplete prefix. It verifies regular single-link topology, the
reservation checksum and every referenced attachment byte, the persisted
evidence checksum and entry identity, and fresh Discord equivalence. It then
rewrites canonical/raw records from the fresh collector, verifies the prior
evidence against the rewritten generation, and removes only the obsolete
reservation/live-evidence receipts before creating a new run-bound reservation.
Any manifest, audit receipt, unexplained file, broken dependency, checksum
change, semantic drift, or attachment mismatch still fails closed with zero
cleanup.

Candidate evidence: 450/450 repository tests PASS, including six focused
resume/schema regressions, successful interrupted-prefix recovery, and
tampered-reservation rejection. Exact-commit install and live continuation
remain pending.

Live retry exposed that a newly classified source census makes the old staged
top-level census receipt intentionally stale before the fresh rewrite. The
recovery verifier now permits an in-memory census reclassification solely for
attachment-byte and reservation verification; it reruns the complete record
validator after replacing canonical/raw content from fresh Discord evidence.
All non-census record invariants, attachment sizes/hashes, reservation binding,
message/asset identity, stable live binding, and unknown-field rejection remain
enforced. The interrupted-prefix test now reproduces this exact old-to-new
census transition and passes; live continuation is pending.

## Discord-owned thread activity during materialized resume

The next live retry proved that the only remaining difference among 6,244
records was the forum starter message for the active backup thread. Discord
updates the embedded thread object's `last_message_id`, `message_count`, and
`total_message_sent` whenever status messages arrive, without editing the
starter message or changing its attachments. Those server-maintained values
changed during the roughly five-minute fresh-evidence pass.

The materialized-stage equivalence proof now excludes only the exact Discord-
owned thread activity fields: last-message ID, message/member counters, total
sent count, and archive activity timestamp. The complete source payload is
still preserved and validated. Thread ID, name, parent/guild identity, flags,
archived/locked state, auto-archive policy, rate limit, starter content,
attachments, and every non-activity field remain in the stable proof and any
change still rejects reuse.

Candidate evidence: focused positive/negative regressions 2/2 PASS and the
complete repository suite 451/451 PASS. The deterministic runtime manifest and
Skill artifact were rebuilt after the source change. Exact-commit install and
live continuation remain pending.
