# Internal Backup Alias Compatibility Task

## Goal

Allow the reviewed Daily Backup installer to upgrade an existing archive that has a
single safe, internal directory alias without weakening the existing no-follow and
rollback guarantees.

## In scope

- Raw-tree integrity hashing for internal directory aliases.
- Positive and hostile-path regression tests.
- Exact OpenClaw agent-message readback normalization discovered during the
  rollback-safe live retry.
- Full release and live cutover verification.

## Out of scope

- Moving or deleting archive data.
- Changing state `relativePath` values.
- Allowing external, broken, chained, file, or unowned links.
- Changing schedules, message limits, report routing, or Qwen behavior.

## Acceptance

- Existing live alias passes the pre-mutation tree hash.
- Unsafe aliases fail before cron mutation.
- Full repository self-check passes.
- Staged agent prompts match OpenClaw's durable contract after its single terminal
  newline normalization.
- Independent review reports no open P0-P3.
- One-shot command and persistent-session canaries omit the cron-only
  `--exact` flag required by OpenClaw 2026.7.1-2; recurring production jobs
  retain their exact schedules.
- Canary cleanup tolerates an inventory-to-delete disappearance race only when
  an authoritative follow-up inventory proves the declaration is absent.
- The persistent-session overlap canary supports OpenClaw's single-flight
  behavior: one simultaneous manual trigger may be rejected, but it must pass
  a bounded retry after the first flight and the final trace must prove two
  complete, non-overlapping executions.
- Live retry returns READY, exact 11/11 readback passes, and legacy jobs remain
  disabled rather than deleted.
