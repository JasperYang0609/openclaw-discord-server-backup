# Installer-owned cron topology

The installer and `manifests/owned-cron.v1.json` are the only sources of truth for
customer cron creation and upgrades. Do not hand-create this topology during a
normal installation.

- 05:10 core workspace backup
- 05:25 deterministic channel/thread discovery
- 05:30, 05:40, 05:50 deterministic rich daily sync under one shared file lock
- 06:10 caught-up audit
- 07:05 one plain-language health report
- 23:10, 00:10, 01:10, 02:10, 03:10, 04:10 bounded backlog runs
- Sunday 13:50 full inventory audit
- Sunday 14:10 immutable-evidence raw reconcile
- Day 1 of each month at 07:00 workspace recovery snapshot

Routine technical jobs use silent delivery. Only the daily health report announces.
Every job alerts after one real error with a one-hour cooldown; safe lock skips do
not increment the error counter.

## Normal install

```bash
python3 scripts/install.py \
  --workspace /absolute/customer/workspace \
  --server-name "CUSTOMER_SERVER" \
  --guild-id "CUSTOMER_GUILD_ID" \
  --report-to "channel:CUSTOMER_REPORT_CHANNEL_ID" \
  --agent main \
  --timezone Asia/Taipei
```

The installer validates the complete cron inventory, stages jobs disabled, runs
isolated-command and shared-file-lock canaries, disables any explicitly adopted
legacy jobs, enables the full desired set last, then verifies the exact result.
Any failure rolls the transaction back.

Use `--offline-scaffold` only when the OpenClaw gateway is intentionally
unavailable. It creates files but returns `PARTIAL_MANUAL_ACTION`; it must never be
reported as a ready installation.

## Existing jobs and adoption

Unknown or look-alike jobs are never deleted automatically. The installer reports a
collision and stops. To adopt an allowlisted legacy job, first fingerprint the exact
job ID, then prepare an explicit adoption map using
`examples/adoption-map.example.json`. Adoption verifies ID, SHA-256 fingerprint,
role marker, schedule, timezone, payload kind, and any allowlisted legacy
declaration key.
