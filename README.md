# OpenClaw Discord Server Backup

OpenClaw-specific skill for reliable Discord server/channel/thread backup.

It uses V3 cursor state, explicit backlog queue, deterministic workers, and live audit probes so high-volume channels do not silently fall behind. It also ships a deterministic core-workspace backup engine for root-level Markdown files and `memory/`, with exact SHA-256 manifests and an isolated restore canary.

## Core guarantee

A channel/thread is caught up only when `read after=<cursor>` returns 0 messages. `lastBackup` is not completion proof.

## Install

Clone this repo, then run the installer with Python 3:

`skill/openclaw-discord-server-backup/scripts/install.py --workspace ~/.openclaw/workspace`

Install `openclaw-lancedb-knowledge` first if the customer wants searchable memory. Then edit the generated backup config and add the OpenClaw cron jobs from `examples/cron.examples.md`.

The default backlog topology is night-only: bounded runs at 23:10 and hourly from
00:10 through 04:10 (Asia/Taipei). It deliberately avoids daytime catch-up work and
stops before the 05:15–06:30 daily backup pipeline. Remaining queue debt carries to
the next night; operators should not raise worker limits to force one oversized run.

## Contents

- `skill/openclaw-discord-server-backup/` - installable OpenClaw skill
- `examples/` - config/state/queue examples and cron examples
- `tests/` - lightweight script tests
- `dist/` - packaged `.skill` artifacts

## Safety

Do not commit tokens, OpenClaw config files, or customer backup data. Only commit templates, scripts, prompts, references, and tests.

## Post-Run Self-Check

After changing queue, cursor, audit, or backlog behavior, run:

```bash
python3 skill/openclaw-discord-server-backup/scripts/post_run_check.py
```

The same script is safe to run from an installed or extracted `.skill` package. It auto-detects the layout: repository clones receive package parity plus the full test suite, while installed packages receive required-file, deterministic smoke, Python compile, and CLI entry-point checks without assuming `tests/` or `examples/` exist nearby.

The check validates example JSON, selector behavior, backlog worker invariants, core-workspace backup/verify/restore-canary behavior, packaged source parity, and the test suite when `pytest` is available. Treat failure as a backup correctness issue, because `lastBackup` alone is not proof that a channel/thread is caught up.

## Maintainer use of Codex

This project is maintained as part of the OpenClaw ecosystem. We plan to use Codex to help review pull requests, reproduce backup edge cases, expand regression tests, and keep release notes accurate when OpenClaw channel, thread, or message APIs change.

API-assisted maintenance should focus on safe, auditable workflows: issue triage, test generation, compatibility checks, documentation updates, and release automation. Codex should not be used to process private Discord exports, customer secrets, or local backup data.

## Raw archive integrity

`caught_up` only proves that no message exists after the stored cursor. It does not prove the historical raw archive contains that cursor or earlier messages. Use `scripts/reconcile_raw_archive_v3.py` to audit raw Markdown against state and, when authorized, re-fetch full current Discord history while preserving existing files.

For persistent protection, schedule `scripts/weekly_raw_reconcile_v4.py --compact`. It adds recovery evidence, bounded append-only repair, full closeout, local-only classification, and a report-channel self-drift guard around the heavier historical scan.

## Guild inventory integrity

Raw reconciliation audits every entry already present in state. Use `scripts/audit_discord_inventory_v3.py` to independently enumerate visible text channels plus active and archived threads, then compare their stable IDs with state. A backup is not server-complete while `missingFromState` is non-zero. The audit reports active and archived totals separately; if archived pagination or permissions are incomplete, the archived total is `null`, the enumeration status is `incomplete`, and the audit fails closed instead of reporting a misleading zero.

For customized deployments, write `--mapping-ledger-out` before apply. Existing stable-ID mappings retain their current `relativePath`; new paths must pass collision and traversal gates.

## Recovery assets

The core backup intentionally covers root Markdown and `memory/`, not every workspace project. Use `scripts/backup_workspace_assets.py` to create a checksummed snapshot of explicitly selected recovery-critical folders such as records, scripts, skills, and a local LanceDB project. The tool refuses to overwrite an existing daily snapshot and excludes dependencies, Git internals, `.env`, and generated Python cache files by default.

Compatibility adapters, wrappers, classification config, and mapping ledgers must also be captured with `scripts/snapshot_deployment_assets.py` and pass an isolated restore canary.
