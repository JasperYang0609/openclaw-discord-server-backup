# Daily Backup + Qwen natural-schedule closeout

## Purpose

Finish the already-authorized production handover only after the first natural
schedule window proves that the installed jobs work without manual triggering.
This is a validation and closeout task, not authority to weaken gates or change
the managed contracts.

## Fixed baselines

- Daily Backup branch: `fix/internal-backup-alias-20260908`
- Daily Backup release commit: `3c8c0eb5318865ccec70ddc2881878abcdb42152`
- Qwen release commit: `d4eff6699ff1c1e8ce47385b290300a60c325ce6`
- Daily live transaction: `memory/openclaw_discord_backup_health/transactions/20260907T183249531686Z`
- Prepared legacy receipt: `memory/openclaw_discord_backup_health/transactions/20260907T183233048861Z`
- Daily managed jobs: 11 enabled; nine adopted legacy jobs must remain disabled.
- The only routine Daily delivery is the 07:05 `health-report` job.

## Natural window to verify (Asia/Taipei, 2026-09-08)

- 03:10 and 04:10: backlog worker (safe `skipped` is acceptable when queue is empty)
- 05:10: core backup
- 05:25: discovery
- 05:30 / 05:40 / 05:50: Daily Sync batches 1–3 on one shared persistent session
- 06:10: caught-up audit
- 06:30: Qwen incremental index
- 06:50: Qwen verified snapshot
- 07:05: one human-readable health report

## Required validation

- Inspect authoritative cron run history, not just file timestamps.
- Daily topology verify must report 11 unchanged, zero create/update, no duplicate
  keys or unknown collisions. Use the interpreter bound by the installer:
  `/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9`.
- Confirm 11/11 managed Daily jobs enabled, 9/9 adoption-map jobs disabled, zero
  temporary canary/diagnostic declarations, and only `health-report` announces.
- Run the installed `post_run_check.py`.
- Run `./qwen-local verify-openclaw` in the Qwen repository and require `ok=true`,
  committed phase, unique incremental/snapshot jobs, ready index, loaded plugin,
  and healthy receipt.
- Confirm the new 06:10 audit, 06:30 index, 06:50 snapshot, and 07:05 report
  receipts are fresh and contain no unhandled anomaly/error.
- Confirm the active backlog queue is zero or explain every remaining item.
- Preserve the restore evidence already passed:
  - Qwen snapshot: 70 files, 104,336 database rows, isolated restore PASS.
  - Core snapshot: 1,023 files, isolated restore PASS.
- Check that no routine duplicate report was delivered.

## Closeout

- If every gate passes, update the security/task evidence with natural-run log
  paths, commit and push the closeout, append a durable summary to
  `memory/2026-09-08.md`, and report COMPLETE in concise Traditional Chinese.
- If any gate fails, report PARTIAL/BLOCKED with the exact failing stage and keep
  the safe current topology. Do not claim completion and do not silently change
  schedules, tools, providers, or safety requirements.
- Never expose secrets, tokens, raw customer content, or full message bodies.
