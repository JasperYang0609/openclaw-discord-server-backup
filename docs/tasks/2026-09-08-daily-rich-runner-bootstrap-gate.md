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
