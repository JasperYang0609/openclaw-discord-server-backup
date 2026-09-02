# Customer Desktop Backup Root Security Scope

Date: 2026-09-02
Target: local Python installer and deterministic backup scripts

## Trust boundaries and data classes

- Untrusted input: server display name, workspace path, Desktop path, custom backup root.
- Sensitive local data: workspace Markdown, `memory/`, Discord archives, local config/state.
- Trusted code boundary: packaged installer and existing core backup engine.
- External systems: none during installation; cron and Discord API mutation remain out of scope.

## Abuse cases

- Path traversal or separator injection writes outside the selected Desktop.
- A symlink redirects writes into an unrelated location.
- Workspace and backup roots overlap and recursively copy or overwrite data.
- Reinstall silently moves, replaces, or deletes an existing backup tree.
- Config and state disagree, causing backup writes to split across roots.
- Test or package artifacts accidentally include secrets or customer data.

## OWASP Top 10:2025 verification register

- A01 Broken Access Control — PASS: workspace containment and non-overlap tests pass;
  no customer cron or Discord API mutation is implemented.
- A02 Security Misconfiguration — PASS: installer integration test proves absolute
  config/state parity and the expected Desktop layout.
- A03 Software Supply Chain Failures — PASS: deterministic package parity and forbidden
  package-entry scan pass; this change adds no third-party dependency.
- A04 Cryptographic Failures — PASS: existing SHA-256 manifest verification runs in the
  full post-run check and core integration test.
- A05 Injection — PASS: empty, traversal, separator, NUL/control-shaped names are rejected
  before writes.
- A06 Insecure Design — PASS: existing-root mismatch stops with an explicit migration
  requirement; no automatic move, overwrite, or deletion exists.
- A07 Authentication Failures — NOT_APPLICABLE_WITH_EVIDENCE: installer accepts and emits
  no credentials and performs no authentication.
- A08 Software/Data Integrity Failures — PASS: package parity, immutable daily snapshot,
  exact manifest verification, and isolated restore canary pass.
- A09 Logging and Alerting Failures — PASS: structured install output contains only paths
  and next steps; blockers are explicit; changed-content secret-shape scan passes.
- A10 Exceptional Conditions — PASS: file, symlink, overlap, unsafe name, rerun, custom
  root, and existing-root mismatch cases are covered by fail-closed tests.

## Verification evidence

- Full suite: 70 tests passed.
- Repository post-run check: PASS, including package parity and core restore canary.
- Python compile and diff hygiene: PASS.
- Release package secret/forbidden-entry scans: PASS.
- Package SHA-256: `9eb67d5679c6aa97174bf25d49817247b5542e4a6aa96a287fc58c090685ff8e`.
- P0/P1 blockers: none.
