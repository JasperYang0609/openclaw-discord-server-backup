# Daily-sync executable-mode release repair

Date: 2026-09-06

## Context

Two helpers invoked directly by the daily-sync prompt were executable in the Skill
source, but the tracked `.skill` artifact still stored them as non-executable. The
installer and post-run check compared bytes without treating execute bits as release
identity, so a mode-only drift could be skipped and reported ready.

## Approved repair

- Normalize each distributable regular file to release mode `0755` only when the
  owner execute bit is set, otherwise `0644`; do not use effective access, group/
  other execute bits, or umask.
- Persist the Unix regular-file type and normalized mode in deterministic ZIP
  `external_attr` metadata, with `create_system=3`.
- Include normalized file mode in the installer Skill-tree hash and verify that the
  staged copy has the same content-and-mode identity as its source.
- Require `check_daily_sync_gate.py` and `backup_health_report.py` to carry execute
  bits in both repository and installed self-check layouts.
- Compare both bytes and normalized file modes when validating the tracked package.

## Acceptance

- The archive records both directly invoked helpers as `0755`, and extraction that
  honors ZIP metadata restores their execute bits.
- A source/target `0755` versus `0644` difference changes `skill_tree_hash` and
  triggers a transactional Skill swap that converges the target mode.
- An installed helper forced to `0644`, `0655`, or `0641` fails the self-check.
- A package member forced to `0644`, non-Unix host metadata, non-regular file
  type, or a duplicate name fails source/package parity.
- Targeted and complete tests, post-run check, package parity, diff checks, and the
  candidate secret scan pass after rebuilding the tracked artifact.

## Boundaries

No schedule, cron ownership, customer data, backup cursor, archive, live install, or
deployment is changed. This repair remains an uncommitted review candidate until the
human release gate authorizes commit and rollout.

## First-adoption health verification addendum

The 07:05 health runner must forward both the configured adoption map and its
prepared-adoption receipt to topology verification. After the installer quiesces
legacy jobs, their current disabled hashes intentionally differ from the original
adoption map; only the private checksummed prepared receipt authorizes that exact
transition. A prepared receipt without its adoption map fails closed.

## Validation recorded 2026-09-07

- Package and installer mode-repair tests: `18 passed`.
- Broader package, installer, health-runner, and topology target: `73 passed`.
- Complete suite: `232 passed`.
- Repository post-run check: all gates passed, including direct-helper executable
  checks and package content/mode parity.
- A second deterministic build was byte-identical to the tracked artifact; archive
  inspection reports both direct helpers as Unix regular files with mode `0755`.
- First-adoption health verification forwards the configured map and prepared
  receipt; an orphan prepared receipt fails closed.
- Diff whitespace and candidate secret-shape scans passed.
