# Contributing to OpenClaw Discord Server Backup

Thanks for helping improve this OpenClaw ecosystem skill. This project focuses on reliable Discord server/channel/thread backup with explicit cursor state, backlog recovery, deterministic workers, and audit probes.

## Before opening an issue

Please include:

- OpenClaw version
- Provider/channel surface involved, for example Discord channel or thread
- Whether the problem affects discovery, cursor advancement, backlog recovery, audit checks, or packaging
- A minimal reproduction using sanitized example data

Do not include tokens, cookies, Discord exports, private channel content, customer backup files, or local OpenClaw configuration.

## Pull request guidelines

- Keep changes focused and reviewable.
- Update README, CHANGELOG, examples, or reference docs when behavior changes.
- Add or update tests for cursor, queue, migration, or audit behavior when possible.
- Preserve the core guarantee: a channel/thread is caught up only when a read after the cursor returns zero messages.
- Avoid introducing dependencies that make customer installs fragile.

## Local checks

Run the lightweight tests before opening a PR:

```bash
python3 -m pytest tests
```

If pytest is not available, document which checks were skipped and why.

## Maintainer workflow

Maintainers may use Codex or other AI coding tools for issue triage, PR review, test generation, documentation updates, and release-note drafting. Do not use AI tools with private customer data, secrets, raw exports, or sensitive local paths.
