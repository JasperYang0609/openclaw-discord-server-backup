# Fresh-Install Automation Closure Security Gate

Date: 2026-09-04

Scope: local OpenClaw Skill installer, cron topology, backup files, receipts, health reports, and restore canaries

Target: OWASP Top 10:2025 A01-A10; ASVS v5.0.0 is `N/A_WITH_REASON` because this repository exposes no web application or network API.

## Security scope

- Assets: Discord raw archives and summaries, cursor/queue state, core and workspace snapshots, cron declarations, rollback receipts, local health evidence, and customer configuration.
- Trust boundaries: CLI arguments, existing OpenClaw cron inventory, adoption maps, packaged Skill contents, filesystem paths, local component receipts, and renderer output.
- Data classification: raw Discord content and customer configuration are private customer data; state and receipts are operational metadata; templates, source, tests, and documentation are public-safe.
- Forbidden flows: no customer data or secrets may enter Git, Discord health output, rollback receipts, or cloud services. Unknown cron jobs must not be changed automatically.
- Abuse cases: declaration-key collision, path traversal or symlink redirection, forged/tampered receipts, concurrent jobs advancing cursors, partial cron mutation, executable substitution, hostile message text interpreted as instructions, hardlink/rename races during snapshot creation, and a false-green report after a skipped or not-yet-due run.

## OWASP Top 10:2025 register

### A01 Broken Access Control — PASS

- The manifest owns only exact versioned declaration keys. Unknown and look-alike jobs are preserved and collisions block activation.
- Adoption requires an explicit guild-bound map and checksummed prepared receipt.
- Workspace/config paths are constrained to the workspace; backup roots must not overlap the workspace; snapshot includes reject absolute, traversal, duplicate, parent/child-overlapping, and symlinked paths.
- Receipt and lock reads use no-follow semantics and validate file type, ownership, link count, and permissions.
- Evidence: `tests/test_cron_topology_manager.py`, `tests/test_installer_desktop_backup.py`, `tests/test_installer_transaction.py`, `tests/test_backup_workspace_assets.py`.

### A02 Security Misconfiguration — PASS

- The versioned manifest specifies schedules, timezone, session mode, notification policy, failure alert threshold/cooldown, and tool policy for every owned job.
- Ready install requires explicit customer identity. Offline scaffold is reported as partial and creates no cron jobs.
- Verification requires exactly one enabled job for every declaration and exact contract parity. Legacy `toolsAllow` on `agentTurn` is removed with the supported edit; command payloads carrying the field and environment fields on owned jobs fail before mutation because they cannot be safely restored from rollback receipts.
- Evidence: `skill/openclaw-discord-server-backup/manifests/owned-cron.v1.json`, `tests/test_cron_topology_manager.py`, `tests/test_installer_desktop_backup.py`.

### A03 Software Supply Chain Failures — PASS

- Runtime uses the packaged scripts and fixed Python/OpenClaw executable paths instead of executable values from customer configuration.
- Packaging rejects symlinks and post-run validation checks required files plus source/package byte parity.
- The implementation adds no third-party runtime dependency; tests use the existing Python/pytest toolchain.
- Evidence: `skill/openclaw-discord-server-backup/scripts/install.py`, `skill/openclaw-discord-server-backup/scripts/package_skill.py`, `skill/openclaw-discord-server-backup/scripts/post_run_check.py`, `tests/test_managed_component_runner.py`.

### A04 Cryptographic Failures — PASS

- Transaction and rollback contracts are canonical JSON with SHA-256 sidecars and are verified before rollback or prepared-adoption reuse.
- Weekly raw pre-repair evidence and workspace snapshot manifests include SHA-256; verification and restore canaries compare content before declaring success.
- Secrets and message bodies are excluded from cron rollback receipts and bounded command diagnostics.
- Evidence: `tests/test_cron_topology_manager.py`, `tests/test_weekly_raw_reconcile_v4.py`, `tests/test_backup_workspace_assets.py`.

### A05 Injection — PASS

- Subprocesses use fixed argv lists with no shell interpolation. Executables are resolved and validated before use.
- Manifest/config values are validated as data; message content is never executed or interpreted as installer instructions.
- Health output is generated from strict schemas plus producer/declaration allowlists, not arbitrary receipt text.
- The optional LanceDB bridge splits legacy command strings into argv and invokes them with `shell=False`; structured argv is also supported.
- Evidence: `tests/test_cron_topology_manager.py`, `tests/test_managed_component_runner.py`, `tests/test_backup_health_report.py`, `tests/test_run_lancedb_incremental.py`.

### A06 Insecure Design — PASS

- Writes precede cursor advancement; daily sync fails closed on incomplete discovery or busy lock.
- Existing upgrades quiesce owned/adopted jobs before filesystem mutation and hold shared locks through mutation and rollback. A running owned/adopted job blocks direct mutation.
- Cron apply is receipt-first and transactional; partial failures trigger checked rollback. Final inventory readback must prove exact owned/adopted restoration, temporary-job removal, and preservation of pre-existing unknown jobs, including when a CLI operation falsely returns success. If rollback certainty is lost, the installer preserves the compatible runtime/config and reports an incomplete rollback instead of performing an unsafe mixed rollback.
- Weekly/monthly first cycles are explicitly `pending`, never false green. Backlog remains bounded and night-only.
- Evidence: `tests/test_installer_transaction.py`, `tests/test_cron_topology_manager.py`, `tests/test_backup_health_report.py`, `tests/test_managed_component_runner.py`.

### A07 Authentication Failures — NOT_APPLICABLE_WITH_EVIDENCE

- This repository implements no authentication endpoint, account session, credential store, or remote service. OpenClaw/Discord authentication remains outside this Skill and no token is accepted or persisted by the installer.
- Identity used for topology scoping (`guildId`, report target, agent/account selector) is explicit configuration and is not treated as user authentication.

### A08 Software or Data Integrity Failures — PASS

- Atomic owner-only writes, checksum-verified receipts, package parity, strict producer/key allowlists, source stability checks, destination inventory checks, and restore canaries protect installation and backup evidence.
- Snapshot/weekly reads reject symlinks, non-regular files, multi-linked sources, inode rebinding, and size/mtime changes during copy.
- State/queue/customer archives are preserved on upgrade and rollback tests verify no destructive cleanup.
- Evidence: `tests/test_backup_workspace_assets.py`, `tests/test_weekly_raw_reconcile_v4.py`, `tests/test_backup_health_report.py`, `tests/test_installer_transaction.py`.

### A09 Security Logging and Alerting Failures — PASS

- Managed jobs write private bounded logs and structured component receipts. One daily report summarizes health in plain Traditional Chinese; cursor/per-entry details remain local.
- Every owned job has a one-error failure alert with cooldown. Exact structured lock skips are recorded as a warning and return success; unrelated failures cannot masquerade as safe skips.
- Health status distinguishes `ok`, `warning`, `pending`, and `error`, includes exact data-loss enums, and requires fresh daily evidence plus bounded weekly/monthly evidence.
- Evidence: `tests/test_managed_component_runner.py`, `tests/test_backup_health_report.py`, `skill/openclaw-discord-server-backup/manifests/owned-cron.v1.json`.

### A10 Mishandling of Exceptional Conditions — PASS

- Tests cover add/update/readback failure, cron/Skill/file rollback failure, no-op-success restore/remove/re-enable faults, final-result write failure, running-job rejection, duplicate/collision inventory, tampered receipts, safe lock skips, stale/pending evidence, source races, hardlinks, symlinks, and overlapping snapshot includes.
- Temporary canaries pre-register declaration keys and perform cleanup even when add readback fails. Optional final result recording cannot invalidate an already committed and verified transaction.
- Restore is canary-only during installation/verification; no production restore or archive deletion occurs automatically.
- Evidence: `tests/test_cron_topology_manager.py`, `tests/test_installer_transaction.py`, `tests/test_backup_workspace_assets.py`, `tests/test_weekly_raw_reconcile_v4.py`.

## Attacker-perspective closeout

- Cross-customer/cross-guild mutation: blocked by exact declaration ownership, guild-bound adoption, and collision gates.
- Privilege and tool expansion: manifest contracts are exact; legacy tool overrides are cleared; environment-bearing owned jobs are blocked before mutation.
- Cost/resource abuse: backlog is bounded and restricted to 23:10-04:10; normal sync jobs serialize through one persistent session and a shared lock.
- Prompt injection/model output: message text is archival data only; cron inventory and receipts are schema-validated; no model output controls executable paths or filesystem destinations.
- Fault recovery: receipt-first transactions, verified rollback, quiescence, file snapshots, atomic writes, and fail-closed incomplete-rollback status prevent silent split-brain state.
- Data egress: no cloud fallback or raw-content health output was introduced; public package contains templates and code only.

## Release gate

Release requires the complete pytest suite, post-run self-check, package/source parity, archive integrity test, JSON validation, Python compilation, diff whitespace check, secret-pattern scan, clean review, and no open P0/P1. The final command evidence is recorded in `logs/tool-runs/` and referenced in the delivery report; logs are not committed.
