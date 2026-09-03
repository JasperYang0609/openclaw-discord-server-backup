# Backup Automation Repair — 2026-09-03

## Scope

Repair production backup automation issues found during a full local audit without deleting raw archives, state history, queues, or immutable snapshots.

## Required changes

- Preserve fair selection between null-cursor bootstrap entries and stale healthy entries.
- Treat `backupExcluded`, `invalidChannel`, and `syncStatus=excluded` as terminal across selector, worker, audit, and queue handling.
- Replace LLM-based discovery with the deterministic inventory audit command.
- Upgrade weekly raw reconciliation from V3 apply-only behavior to the V4 recovery-first closeout workflow.
- Keep the three daily sync jobs on one custom session so restart catch-up cannot run them concurrently; each run must reload current state and queue before selecting work.
- Repair incomplete local config and add the active Qwen project to the monthly workspace snapshot.
- Add explicit failure alerts for enabled backup jobs.

## Safety gates

- Create and checksum a pre-repair recovery snapshot before mutations.
- Never delete or rewrite Discord raw archives.
- Never overwrite an immutable daily snapshot.
- Keep old Gemini assets for rollback while creating a separate Qwen snapshot root.
- Run regression tests, self-checks, state/queue validation, deterministic inventory, and remote parity checks before closeout.

## Security scope (OWASP Top 10:2025)

- A01 PASS: archive paths remain containment-checked and excluded/invalid entries are terminal.
- A02 PASS: pre-repair recovery evidence and packaged artifacts are checksum-verified.
- A03 PASS: deterministic cron commands use fixed arguments; message IDs are handled as numeric snowflakes.
- A04 PASS: bounded workloads, fair selection, frozen report cutoff, and append-only repair cover abuse and moving-target cases.
- A05 PASS: daily jobs share one session key, reload durable state, and fail closed on unsafe or incomplete results.
- A06 PASS: no new dependency was added; existing dependency and package checks remain green.
- A07 NOT_APPLICABLE_WITH_EVIDENCE: authentication and identity flows are unchanged.
- A08 PASS: Raw is written before cursors advance; immutable snapshots and recovery evidence are not overwritten.
- A09 PASS: enabled backup jobs have explicit redacted failure alerts and bounded diagnostics.
- A10 PASS: no new outbound integration is introduced; Discord data remains in the configured local archive.

Web/API ASVS register: NOT_APPLICABLE_WITH_EVIDENCE. This change is a local CLI and cron automation repair and exposes no web or API endpoint.

## Completion evidence

- Repo test suites pass and implementation commits exist.
- Installed/runtime scripts match the reviewed source or have an explicitly documented local compatibility merge.
- Exactly one enabled Qwen incremental cron remains.
- Qwen incremental audit, snapshot checksum, restore canary, DB-open, row-count, freshness, and retention gates pass.
- Discord queue has no active errors, config is initialized, inventory has no missing entries, and weekly V4 command is installed.
