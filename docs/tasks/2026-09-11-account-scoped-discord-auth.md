# Account-scoped Discord authentication repair

Date: 2026-09-11

## Incident

The managed backup jobs failed after the OpenClaw Discord token moved from the
legacy channel-level field to `channels.discord.accounts.<accountId>.token`.
The backup configuration already identifies `accountId=default`, but the
managed runner did not resolve that account before starting deterministic
Discord helpers. Core-file backup remained healthy; Discord discovery, daily
sync, audit, and backlog were affected.

## Scope

- Resolve the configured Discord account token in the managed parent process.
- Pass it to Discord child helpers only through the child environment.
- Preserve environment-variable precedence and the legacy channel-level token
  fallback.
- Keep tokens out of argv, receipts, summaries, and controlled logs.
- Rebuild and deploy through the transactional installer; do not hand-edit cron
  jobs or archive state.

## Out of scope

- Token rotation, account login, permission expansion, schedule changes, cursor
  rewrites, archive deletion, or backup-root migration.
- Resolving arbitrary OpenClaw SecretRef providers inside this skill.

## Acceptance

- Regression tests prove configured-account selection, environment precedence,
  legacy fallback, and non-disclosure in logs and receipts.
- Complete repository tests, post-run check, package parity, diff check, and
  secret-shape scan pass.
- Transactional installer returns READY and exact topology verification passes.
- Installed discovery, three bounded daily-sync batches, caught-up audit, and
  health report succeed; current stock report remains independently healthy.

## Security scope

- Trust boundary: local OpenClaw config to a bounded child-process environment.
- Sensitive data: Discord bot token; never persist or print it.
- Abuse cases: selecting the wrong account, accepting non-string secret objects,
  leaking a token through argv/logs, or broadening job permissions.
- Rollback: installer transaction receipt plus the prior Git commit; backup
  state and archive bytes are not modified by the code deployment itself.
- OWASP A01/A02/A04/A05/A08/A09/A10: in scope and require regression/evidence.
  A03/A06/A07: not applicable with evidence (no user query construction, new
  dependency, or interactive identity flow).

## Candidate evidence

- Targeted managed-runner tests: 18 PASS.
- Complete repository suite: 467 PASS.
- Repository post-run check: PASS, including package/source parity and runtime
  component manifest verification.
- Python compilation, `git diff --check`, and changed-content secret-shape scan:
  PASS; no dependency was added.
- OWASP A01/A02/A04/A05/A08/A09/A10: PASS through fixed account selection,
  child-environment-only credential transport, fail-closed object handling,
  deterministic package/hash verification, bounded diagnostics, and regression
  evidence. A03/A06/A07: `N/A_WITH_EVIDENCE` for the reasons above.

## Live deployment evidence

- Release commit deployed: `41e30a6`.
- Transactional installer: `READY`; 11/11 owned jobs updated and verified.
- Installed-layout post-run check: PASS, including 28 compiled scripts and 17
  CLI entry-point checks.
- Manual incident recovery through the installed cron payloads: discovery PASS;
  daily-sync batches 1–3 PASS; caught-up audit PASS; bounded backlog PASS.
- Final active queue: 0. Consolidated health reports core files complete,
  channels/threads caught up, search index current, and topology/alerts normal.
- The next natural schedule remains the final regression observation; no
  recurring schedule, permission, backup root, or customer archive was changed.
