# Local Backup → Incremental Index → Search Closure Task

Status: authorized; implementation in progress.

## Objective

Restore and prove the local production chain:

`Discord raw incremental backup → local Qwen/LanceDB incremental index → sourced search result`

Client packaging and optional full rich-archive migration are explicitly out of scope.

## Required changes

- Separate core/raw backup health from optional rich-archive readiness.
- Catch up only entries with verified messages after their durable cursor.
- Preserve append-only raw archives and advance cursors only after write/readback.
- Preserve rich queue/state evidence while core work progresses.
- Prevent legacy duplicate message IDs from creating duplicate indexed results.
- Keep Qwen model and index identity unchanged.

## Non-goals

- No 182-entry rich archive rebuild.
- No client installer or transfer package.
- No cloud embedding provider.
- No deletion of unknown cron jobs, source history, or backup evidence.
- No parallel workers on the current 16 GiB host.

## Evidence required

- Git preflight and clean closeout.
- Focused regression tests and full repository suite.
- Package/source parity, dependency audit, secret scan, and diff checks.
- Read-only live audit identifying exact core debt and rich-only warnings.
- Installed post-run check and exact cron topology readback.
- Manual backup catch-up followed by a zero-after-cursor audit.
- Fresh incremental index receipt and sourced top-five retrieval proof.
- Isolated restore verification.
- Natural schedule evidence from the next 05:25–07:05 window.

## Stop conditions

- Any unexplained cursor movement, missing raw history, or destructive rewrite.
- Any mutation outside managed jobs or approved local backup state.
- Any model/index identity drift.
- Any P0/P1 security finding or memory pressure unsafe for the host.

## Completion rule

Manual closure may be reported separately from natural-schedule validation. The
local system is not 100% complete until the next natural schedule window passes.

