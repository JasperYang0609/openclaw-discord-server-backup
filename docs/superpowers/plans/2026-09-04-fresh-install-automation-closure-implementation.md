# Fresh-Install Backup Automation Closure Implementation Plan

Date: 2026-09-04
Approved design: `../specs/2026-09-04-fresh-install-automation-closure-design.md`
Approval: Jasper, 2026-09-04 (`規格確認，開始實作`)

## Objective

Turn the repository into the authoritative, idempotent installer/upgrader for its
owned backup topology, while preserving customer archives and unknown cron jobs.
Replace engineering-style Discord chatter with one daily, plain-language health
report; detailed per-entry evidence remains local and failures remain actionable.

## In scope

- Ship and validate a versioned owned-cron manifest.
- Add deterministic `plan`, `apply`, and `verify` reconciliation with checksummed
  receipts and transaction rollback.
- Install exact schedules, a shared daily-sync session, failure alerts, deterministic
  discovery, Daily Sync Gate, night-only backlog, weekly repair/audit, workspace
  snapshot, and one consolidated health-report job.
- Preserve unknown/look-alike jobs and block activation on collisions.
- Add Weekly Raw pre-repair checksum verification and workspace snapshot
  create/verify/restore-canary operations.
- Package all required prompts, scripts, examples, and contracts in the `.skill`.
- Make normal user-visible reporting concise; render lock skips as plain language and
  expose reason, impact, data-loss status, and repair progress only for anomalies.
- Support fresh install, no-op reinstall, upgrade, rollback, offline scaffold, and
  installed-layout verification.

## Out of scope / forbidden

- No archive, state, queue, snapshot, or customer-config deletion.
- No automatic disable/delete of non-owned cron jobs.
- No daytime backlog schedule or larger worker limits.
- No cloud backup, message-content upload, production restore, or secret persistence.

## Delivery batches

1. Characterize current installer, cron CLI schema, package contents, and reporting
   state with tests before changing behavior.
2. Implement manifest validation and pure reconciliation planning.
3. Implement transactional cron apply/rollback and exact verification.
4. Implement recovery-integrity helpers and their tamper/rollback tests.
5. Implement local run-result receipts and the consolidated health renderer/job.
6. Wire installer/upgrade/offline modes and package parity.
7. Exercise temporary-profile fresh install, reinstall, upgrade, collision, injected
   failure, rollback, alert, lock-skip, and uninstall/restore canaries.
8. Synchronize the reviewed Skill locally only after repository tests pass; verify
   config/state/queue/raw hashes and reconcile live owned jobs without touching
   unknown jobs.

## Acceptance evidence

- Exactly one enabled job per owned declaration; all schedules, timezone, payload,
  session, limits, alert policy, and cleared legacy tools fields match the manifest.
- Three daily-sync jobs share one session and fail closed on incomplete inventory or
  busy lock without moving cursors.
- One daily health report presents core backup, channel/thread completeness, local
  index, snapshot/restore, schedules/alerts, and pending work in plain language.
- Normal worker jobs do not emit per-entry `cursor`, `added`, or `processed` chatter
  to Discord; the same evidence is retained in bounded local receipts/logs.
- Package/extracted/install parity and all regression/security tests pass.
- Installer-only upgrade leaves customer data hashes unchanged.

## Security scope and closeout requirements

Assets are cron definitions, ownership receipts, workspace backup state, raw archives,
snapshots, and local reports. Trust boundaries are CLI input, OpenClaw cron JSON,
filesystem paths, packaged files, and Discord reporting. Inputs and tool/model output
are untrusted. The installer may write only owned files/declarations and may never
interpret message content as a command.

OWASP A01-A10 evidence must cover ownership/cross-root controls, exact configuration,
package/dependency/secret checks, SHA-256 receipts, fixed argv and path validation,
transaction rollback, authentication N/A evidence, package/data integrity, end-to-end
alerts, and fault injection. ASVS v5.0.0 is not applicable because this is a local
CLI/Skill/cron product with no Web/API surface. Release is blocked on any missing
A01-A10 evidence, open P0/P1, package drift, rollback failure, or unexpected customer
data hash change.

## Handoff format

Implementer reports changed files, tests/logs, risks, commit candidate, and
`COMPLETE|BLOCKED|NEEDS_PM`. A separate reviewer examines the diff and evidence,
runs independent negative tests, and alone decides release readiness.
