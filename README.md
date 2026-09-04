# OpenClaw Discord Server Backup

OpenClaw-specific skill for reliable Discord server/channel/thread backup.

It uses V3 cursor state, explicit backlog queue, deterministic workers, and live audit probes so high-volume channels do not silently fall behind. It also ships a deterministic core-workspace backup engine for root-level Markdown files and `memory/`, with exact SHA-256 manifests and an isolated restore canary.

## Core guarantee

A channel/thread is caught up only when `read after=<cursor>` returns 0 messages. `lastBackup` is not completion proof.

## Install

Clone this repo, then run the transactional installer with Python 3:

```bash
python3 skill/openclaw-discord-server-backup/scripts/install.py \
  --workspace ~/.openclaw/workspace \
  --server-name "南方" \
  --guild-id "DISCORD_GUILD_ID" \
  --report-to "channel:REPORT_CHANNEL_ID" \
  --agent main \
  --timezone Asia/Taipei
```

For a fresh default macOS installation, `--server-name` is required. The installer
creates one real Desktop folder named `<Discord伺服器名稱>資料備份`, for example:

```text
~/Desktop/南方資料備份/
├── Discord資料/
└── 核心文件/          # created/populated by the core-backup job
    ├── latest/
    └── snapshots/
```

The Discord config and state both point to the absolute `Discord資料/` path. The
existing deterministic core-backup engine receives the parent backup root and owns
`核心文件/`. Use `--backup-root /absolute/custom/path` only when the customer has
explicitly chosen a non-Desktop location; a custom root disables automatic Desktop
folder creation. Existing backup trees are never moved automatically.

The installer owns the full cron topology. It performs complete-inventory checks,
stages jobs disabled, runs command and shared-session canaries, enables the complete
set last, verifies exact contracts, and rolls back on failure. A byte-identical rerun
is a no-op. Unknown or look-alike jobs are reported and left untouched; legacy
adoption requires an explicit ID plus SHA-256 fingerprint map.

Use `--offline-scaffold` only when the gateway is intentionally unavailable. This
returns `PARTIAL_MANUAL_ACTION`, not a ready install. Install the separate local Qwen
knowledge product first when searchable memory is required, and pass only its
explicit receipt path; this installer never guesses another product's files or jobs.

The default backlog topology is night-only: bounded runs at 23:10 and hourly from
00:10 through 04:10 (Asia/Taipei). It deliberately avoids daytime catch-up work and
stops before the 05:10–07:05 daily backup pipeline. Remaining queue debt carries to
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

The check validates example JSON, the owned cron manifest, daily-sync gate, selector
and worker invariants, immutable weekly evidence, core/workspace snapshot verification
and restore canaries, installed CLI entry points, packaged source parity, and the test
suite when `pytest` is available. Treat failure as a backup correctness issue.

## Operator reporting

Routine jobs write private structured receipts and stay silent. At 07:05 the health
job verifies the topology and emits one Traditional Chinese, plain-language report.
It never treats yesterday's daily or Qwen receipt as today's proof, and it never
claims weekly/monthly verification before the first scheduled evidence exists.
Technical fields such as cursors, batch counts, and log paths remain in local logs.

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
