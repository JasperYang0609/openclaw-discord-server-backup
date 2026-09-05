# Fresh-Install Backup Automation Closure Design

Date: 2026-09-04
Decision: Approach A approved by Jasper on 2026-09-04

## Problem

The repository contains the hardened backup engines and operator guidance, but a
fresh customer install still produces only files and JSON scaffolding. It does not
declaratively create or upgrade the complete OpenClaw schedule. A customer can
therefore omit failure alerts, run the three daily-sync slots in different sessions,
use prompt-driven discovery, or install a package that cannot verify the Daily Sync
Gate. Recovery helpers also lack two closed-loop guarantees: Weekly Raw does not
checksum and verify its pre-repair evidence before writing, and workspace snapshots
cannot verify or restore-canary their own output.

## Outcome

`install.py` becomes the single customer entry point for installing or upgrading the
Discord backup product. A successful ready result means the packaged Skill, local
layout, owned cron declarations, failure alerts, deterministic discovery, shared
daily-sync session, integrity gates, and recovery checks have all been verified.

The installer may offer an explicit offline-scaffold mode for packaging or disaster
recovery. That mode must return `PARTIAL_MANUAL_ACTION`, must not claim the system is
ready, and must not create or mutate cron jobs.

## Ownership and configuration

- A packaged, versioned cron manifest is the source of truth. It is shipped inside
  the Skill so an extracted `.skill` is sufficient for installation and verification.
- Each recurring job has a stable declaration key derived from product, guild ID,
  role, and contract version. Renaming a Discord server must not change ownership.
- The three daily-sync declarations have distinct role keys but share one explicit
  isolated session key derived from the same guild ID, which serializes normal runs
  and restart catch-up runs.
- Required customer inputs are Discord guild ID, report destination, OpenClaw agent,
  timezone, workspace, and backup root/server name. IDs and paths are stored only in
  the customer's local config/ownership receipt; templates contain no customer data.
- All command jobs use fixed argv, cwd, timeout, no-output timeout, and output cap.
  Agent jobs use packaged prompt files and the minimum existing tool policy.
- Every owned job enables a failure alert after one execution error, excludes safe
  skipped runs, uses a one-hour cooldown, and sends to the configured report target.
- Compatibility correction (2026-09-05): `--clear-tools` is supported only for
  `agentTurn`. An owned command job containing `payload.toolsAllow` fails before
  mutation and requires a reviewed transactional rebuild from the manifest; fresh
  command declarations omit the field. An isolated command canary must pass before
  activation.

## Managed topology

The manifest owns the product's core workspace backup, deterministic guild
inventory/discovery, three serialized daily-sync slots, night-only backlog worker,
caught-up audit, weekly inventory audit, Weekly Raw V4 reconcile, and workspace
recovery snapshot. The existing 23:10 and 00:10-04:10 backlog window remains fixed;
no daytime backlog job is added.

Discovery invokes the deterministic inventory command and writes a complete dated
inventory report. Daily sync must call `check_daily_sync_gate.py` before selecting
entries and must make no changes if the shared lock is busy or today's inventory is
missing or incomplete.

The Qwen incremental and Qwen snapshot declarations are not owned by this product.
They remain the responsibility of the Qwen-local installer.

## Reconciliation transaction

The cron manager exposes `plan`, `apply`, and `verify` operations.

1. Read the full cron inventory and validate its schema.
2. Match owned jobs only by exact declaration key.
3. Detect non-owned jobs that target the same scripts or session. Preserve them and
   return a blocker with their IDs; never delete or disable them automatically.
4. Save a redacted, checksummed pre-change receipt for every owned job definition.
5. Create missing owned jobs and update drifted owned jobs to the manifest contract.
6. Clear legacy tools fields only on `agentTurn` jobs. Command jobs never receive a
   tools edit; unsafe owned command definitions fail before mutation and must be
   rebuilt through a reviewed transaction. Attach alerts, run the isolated canary,
   and verify the complete topology.
7. On any failure, remove only jobs created by this transaction and restore prior
   owned definitions. Existing state, queue, config, raw archives, snapshots, and
   unknown cron jobs remain unchanged.

Re-running the installer performs an upgrade reconciliation. An unchanged install
must be a no-op with exactly one enabled job per declaration key. A committed older
receipt is not a reason to skip verification or upgrades.

## Recovery integrity changes

### Weekly Raw V4

Before the first append, the workflow creates an immutable evidence bundle containing
the pre-repair state, queue, relevant raw files, and a SHA-256 manifest. It immediately
verifies the bundle. Missing, extra, symlinked, or hash-mismatched evidence blocks all
repair writes. Repair remains append-only and never rewrites existing raw messages.

### Workspace snapshots

`backup_workspace_assets.py` gains explicit create, verify, and restore-canary
operations. Verification enforces the manifest file set, byte size, SHA-256, path
containment, regular-file type, and no symlinks. The restore canary copies only into an
automatically removed temporary directory, re-verifies the result, and never restores
into the live workspace. Existing immutable daily snapshots are never overwritten.

## Package and installed self-check

- The `.skill` archive includes the cron manifest, complete operator examples, all
  managed prompt files, the cron manager, and integrity helpers.
- Repository package parity compares the complete file set and contents.
- Installed-layout `post_run_check.py` requires and executes the Daily Sync Gate,
  cron-manifest validation, ownership/session/alert contract smoke tests, Weekly Raw
  pre-evidence verification, and workspace verify/restore canary.
- Local deployment hashes config, state, queue, and raw roots before and after Skill
  synchronization. Any unexpected change fails the deployment and restores the prior
  installed Skill.

## Failure handling and operator visibility

- Invalid CLI JSON, ambiguous ownership, duplicate owned keys, unknown colliding
  jobs, canary failure, partial cron mutation, or failed integrity verification are
  blockers; the installer must not print a ready result.
- Diagnostics are bounded and redacted. They may include job IDs, declaration roles,
  failure categories, and evidence paths, but never tokens, message bodies, or private
  config values.
- No installer path deletes customer archives, state history, queues, immutable
  snapshots, or non-owned jobs.

## Verification matrix

- Fresh install creates the complete owned topology and passes verification.
- A second identical run creates zero duplicate jobs and changes no data files.
- An older owned install upgrades to the current manifest and can roll back on an
  injected failure at every mutation phase.
- Unknown look-alike jobs are preserved and block activation.
- All three daily-sync jobs share exactly one session and require the Gate.
- All owned jobs have the exact schedule, timezone, payload, limits, and alert policy;
  no owned job contains `payload.toolsAllow`.
- Package and extracted installed-layout checks fail when the manifest, cron manager,
  Daily Sync Gate, or operator examples are missing or drifted.
- Weekly repair refuses to write after evidence tamper, missing/extra files, symlinks,
  or checksum mismatch.
- Workspace snapshot verify and restore-canary reject tamper, missing/extra files,
  symlinks, traversal, and live-target restoration.
- State, queue, config, raw archives, and immutable snapshot hashes remain unchanged
  during installer-only upgrades.

## Security scope and planned OWASP Top 10:2025 evidence

This is a local CLI, Skill, and cron automation product. It exposes no web or API
endpoint, so ASVS v5.0.0 is not applicable; equivalent controls are the CLI trust
boundary, ownership manifest, fixed argv, path containment, atomic receipts, rollback,
and deterministic negative tests.

- A01 Broken Access Control: exact declaration-key ownership and managed-root checks;
  non-owned job and cross-root negative tests.
- A02 Security Misconfiguration: exact schedule/session/alert/tools verification and
  drift tests.
- A03 Software Supply Chain Failures: deterministic package parity, pinned existing
  dependencies, archive inspection, and secret scan.
- A04 Cryptographic Failures: SHA-256 evidence/snapshot manifests and no secrets in
  Git, logs, prompts, or receipts.
- A05 Injection: fixed argv, validated IDs/timezone/paths, no model or customer text
  interpolated into shell commands.
- A06 Insecure Design: fail-closed ambiguity, transaction rollback, immutable evidence,
  append-only repair, and bounded workloads.
- A07 Authentication Failures: not applicable with evidence; the installer does not
  implement authentication or store credentials and relies on the local OpenClaw CLI.
- A08 Software or Data Integrity Failures: package parity, manifests, post-copy verify,
  restore canaries, and rollback receipts.
- A09 Security Logging and Alerting Failures: one actionable alert policy per owned
  job plus an end-to-end failure-alert canary with redacted output.
- A10 Mishandling of Exceptional Conditions: fault injection for invalid JSON,
  timeout, duplicate runs, partial mutation, lock contention, disk failure, rollback,
  and restart/resume.

Release is blocked if any A01-A10 item lacks evidence, any P0/P1 remains open, package
parity fails, or local data hashes change unexpectedly.

## Non-goals

- No change to Discord message retention, raw privacy policy, or `PARTIAL_PRIVACY_GATE_RAW`.
- No automatic deletion or disabling of unknown customer jobs.
- No daytime backlog processing and no increase to bounded worker limits.
- No cloud backup, cloud embeddings, production data migration, or direct restore into
  a live workspace.
