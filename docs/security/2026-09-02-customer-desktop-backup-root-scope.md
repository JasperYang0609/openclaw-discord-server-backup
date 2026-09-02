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

- A01 Broken Access Control: local path containment and no customer cron/API mutation.
- A02 Security Misconfiguration: deterministic absolute paths and config/state parity.
- A03 Software Supply Chain Failures: package parity, source inventory, dependency audit.
- A04 Cryptographic Failures: existing SHA-256 manifest verification retained.
- A05 Injection: reject control characters, NUL, separators, traversal names.
- A06 Insecure Design: fail before writes; no automatic migration or deletion.
- A07 Authentication Failures: not applicable; installer handles no credentials.
- A08 Software/Data Integrity Failures: immutable daily snapshots and restore canary retained.
- A09 Logging and Alerting Failures: structured non-secret install output and explicit blockers.
- A10 Exceptional Conditions: file/symlink/overlap/existing-root failures are tested fail-closed.

Final PASS/BLOCKED/N/A evidence is recorded after implementation tests.
