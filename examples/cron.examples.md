# OpenClaw Cron Examples

Use these as templates. Replace paths and report targets with customer config.

## Discovery

Schedule: daily 05:25.

## Daily sync

Schedule: daily 05:30.

## Backlog worker

Schedule: `10 0,1,2,3,4,6,11,17,23 * * *`.

The prompt should run `scripts/run_backlog_worker_v3.py` with config-derived paths.

## Audit

Schedule: daily 06:30 or 23:30.

The prompt should run `scripts/audit_caught_up_v3.py` and report any false healthy entries.
