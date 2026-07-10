# OpenClaw Discord Server Backup

OpenClaw-specific skill for reliable Discord server/channel/thread backup.

It uses V3 cursor state, explicit backlog queue, deterministic workers, and live audit probes so high-volume channels do not silently fall behind.

## Core guarantee

A channel/thread is caught up only when `read after=<cursor>` returns 0 messages. `lastBackup` is not completion proof.

## Install

Clone this repo, then run the installer with Python 3:

`skill/openclaw-discord-server-backup/scripts/install.py --workspace ~/.openclaw/workspace`

Install `openclaw-lancedb-knowledge` first if the customer wants searchable memory. Then edit the generated backup config and add the OpenClaw cron jobs from `examples/cron.examples.md`.

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

The check validates example JSON, selector behavior, backlog worker invariants, and the test suite when `pytest` is available. Treat failure as a backup correctness issue, because `lastBackup` alone is not proof that a channel/thread is caught up.

## Maintainer use of Codex

This project is maintained as part of the OpenClaw ecosystem. We plan to use Codex to help review pull requests, reproduce backup edge cases, expand regression tests, and keep release notes accurate when OpenClaw channel, thread, or message APIs change.

API-assisted maintenance should focus on safe, auditable workflows: issue triage, test generation, compatibility checks, documentation updates, and release automation. Codex should not be used to process private Discord exports, customer secrets, or local backup data.
