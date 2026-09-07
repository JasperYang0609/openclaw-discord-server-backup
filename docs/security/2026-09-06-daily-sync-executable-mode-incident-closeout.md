# Daily-sync executable-mode incident — OWASP closeout addendum

Date: 2026-09-06

Scope: deterministic Skill packaging, transactional installation, repository and
installed self-checks, and the two helpers invoked directly by daily sync.

ASVS v5.0.0: `NOT_APPLICABLE_WITH_EVIDENCE`; this repository exposes no web
application or network API. The existing local CLI and filesystem trust boundaries
remain in scope.

## Incident and trust boundary

The source tree, packaged archive, and installed tree could hold identical bytes but
different execute bits. Byte-only release identity allowed a stale non-executable
helper to survive an upgrade no-op and allowed the post-run check to report ready.
The repair treats the owner execute bit as integrity metadata. Group/other execute
bits cannot substitute for a missing owner execute bit on an owner-owned install.
It never uses message content, customer configuration, effective access checks, or
umask to decide whether code is executable.

## OWASP Top 10:2025 register

- A01 Broken Access Control — `PASS`: ownership, path, symlink, and transaction
  boundaries are unchanged; 07:05 topology verification forwards the exact
  configured adoption map and checksummed prepared receipt, never an inferred job.
- A02 Security Misconfiguration — `PASS`: both directly invoked helpers now require
  the owner execute bit in source and installed layouts; `0655`/`0641` fail closed.
- A03 Software Supply Chain Failures — `PASS`: deterministic ZIP metadata records
  Unix regular-file type plus `0755`/`0644`; verification rejects non-Unix host
  metadata, duplicate names, non-regular types, and mode/content drift.
- A04 Cryptographic Failures — `PASS`: existing SHA-256 transaction and evidence
  contracts are unchanged; mode identity is added to the installer tree digest.
- A05 Injection — `PASS`: fixed argv and `shell=False` behavior are unchanged; the
  mode decision comes only from local file stat metadata.
- A06 Insecure Design — `PASS`: mode-only drift can no longer take the no-op path;
  staging must match the source content-and-mode digest before transactional swap.
- A07 Authentication Failures — `NOT_APPLICABLE_WITH_EVIDENCE`: this repair has no
  login, session, credential store, or authentication endpoint.
- A08 Software or Data Integrity Failures — `PASS`: source, archive, staged, and
  installed identities now cover executable intent, Unix file type, member
  uniqueness, and file bytes.
- A09 Security Logging and Alerting Failures — `PASS`: self-check output names the
  missing executable invariant or package-mode mismatch and exits non-zero.
- A10 Mishandling of Exceptional Conditions — `PASS`: stale `0644` installs and
  tampered archive modes fail closed; stage mismatch is removed before activation;
  a prepared adoption receipt without its matching map is rejected.

## Evidence and attacker review

- `tests/test_package_skill.py` verifies deterministic archive modes, extraction,
  installed `0644`/`0655`/`0641` rejection, and tampered mode, host metadata,
  file type, and duplicate-member rejection.
- `tests/test_installer_transaction.py` verifies mode-only hash drift and transactional
  swap convergence using explicit permissions independent of umask.
- `tests/test_managed_component_runner.py` verifies 07:05 forwards both adoption
  authorities and rejects an orphan prepared receipt before topology verification.
- The complete test suite, repository post-run check, rebuilt package parity, archive
  inspection, whitespace checks, and candidate secret scan form the release evidence.
- An attacker who can remove execute bits cannot preserve the same installer digest
  or obtain a green self-check. Symlink rejection and transactional rollback remain
  enforced. This change adds no data egress, AI-controlled executable decision, shell
  interpolation, dependency, or live mutation.

## Release decision

Code-level P0/P1 findings are closed when all listed evidence passes. Commit, push,
deployment, and live cron/install mutation remain a separate human gate and are not
authorized by this closeout.
