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

