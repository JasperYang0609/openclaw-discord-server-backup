# Customer Desktop Backup Root Implementation Plan

Date: 2026-09-02
Design: `docs/superpowers/specs/2026-09-02-customer-desktop-backup-root-design.md`
Authorization: Jasper approved implementation on 2026-09-02.

## Goal

Make a fresh macOS customer install create one real Desktop directory named
`<Discord server name>資料備份`, with `Discord資料/` for Discord content and
`核心文件/latest` plus `核心文件/snapshots` produced by the existing core backup
engine.

## Scope

- Add explicit server-name and custom backup-root inputs to the installer.
- Validate names, Desktop paths, symlinks, files, and workspace overlap before writes.
- Create the default Desktop root and `Discord資料/` idempotently.
- Keep config and state roots identical and absolute.
- Preserve existing config/state and report migration requirements instead of moving data.
- Update README, installed skill reference, examples, cron guidance, and changelog.
- Add installer integration tests, core-backup integration coverage, package parity, and
  post-run verification.

## Out of scope

- Moving an existing customer backup tree.
- Enabling or mutating customer cron jobs.
- Reading Discord content or changing cursor/backlog behavior.
- Expanding the core backup source set.

## Validation

- Unit/integration tests use an isolated fake home and Desktop.
- Full repository test suite and post-run self-check pass.
- Packaged `.skill` is rebuilt and byte-matches the source tree.
- Secret scan and dependency/security checks find no release blocker.
- Git worktree is clean after the final commit.

## Stop conditions

Stop without modifying customer data if a destination is unsafe, an existing configured
root differs from the requested root, or a required verification fails.
