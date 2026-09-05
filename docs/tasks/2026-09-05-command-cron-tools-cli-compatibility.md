# Command cron tools CLI compatibility repair

## Context

OpenClaw 2026.7.1-2 accepts `--clear-tools` for `cron edit`, but applying it to a
command payload is rejected because the CLI constructs an `agentTurn` patch without
a message. Freshly-created command jobs already omit `payload.toolsAllow`.

## Goal

Keep the approved fresh-install topology unchanged while avoiding unsupported
command-job tool-list edits. Agent jobs must still clear legacy tool allow-lists.
Because this CLI also ignores command-job tool lists during `cron add`, any legacy
command job containing that field must fail closed before mutation.

## Scope

- Patch command/agent tool handling in `manage_cron_topology.py`.
- Make the tooling audit and shipped operator guidance payload-kind-safe.
- Add regression tests for live-CLI-compatible argument construction and remediation.
- Rebuild the deterministic Skill artifact.
- Do not alter backup data, cursors, paths, schedules, delivery, or ownership keys.

## Acceptance

- Command-job post-add edits contain neither `--clear-tools` nor `--tools`.
- Agent-job post-add edits retain the approved clear/preserve behavior.
- A command job containing `toolsAllow` is rejected before any mutation.
- The audit never recommends a command-job tools edit; `agentTurn` retains the
  supported clear remediation.
- Full tests, package parity, secret scan, and live disabled-canary compatibility pass.

## Security gate

- A01/A07: ownership/adoption and authorization gates unchanged.
- A02/A08: checksummed receipts and deterministic package parity reverified.
- A03/A05: fixed argv and strict payload-kind branching; no shell interpolation added.
- A04/A06/A09/A10: fail-closed rollback, dependency audit, bounded logs, and external
  CLI responses remain validated. No new network or AI trust boundary.
- Authentication is not applicable; OpenClaw Gateway authorization is inherited and
  exercised only through the existing local CLI.

## Stop conditions

Stop and roll back if the live cron inventory, unknown jobs, state/queue/raw hashes,
or an owned job contract changes outside the approved manifest.
