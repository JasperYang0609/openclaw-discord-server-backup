# OWASP Top 10:2025 Gate — Local Backup/Index/Search Closure

Status: in progress. Every item must end as `PASS`, `BLOCKED`, or
`NOT_APPLICABLE_WITH_EVIDENCE` before release.

## Scope and trust boundaries

- Discord API input is untrusted external data.
- Raw archives, durable cursors, queue/state receipts, and the local index are
  integrity-sensitive local data.
- OpenClaw cron and managed installers are privileged local control planes.
- Search output is untrusted model-adjacent output and must retain source
  provenance.
- No secrets or message bodies may enter Git, logs, reports, or test fixtures.

## Data classification

- Discord message bodies and attachments: confidential customer/work data.
- Channel/thread IDs and backup roots: internal metadata.
- Tokens, credentials, signing keys: secret; forbidden from repository output.
- Test counts, hashes, redacted receipts: release evidence.

## Abuse cases

- Forged or stale cursor skips messages.
- Optional rich-archive failure falsely marks core backup failed or healthy.
- Duplicate IDs create duplicate search results or silent overwrite.
- Malicious message content changes paths, commands, or index configuration.
- A replacement installer mutates unknown cron jobs.
- Failed writes advance state or partially replace trusted evidence.
- Parallel workers race on archives, state, or index snapshots.

## A01–A10 register

- A01 Broken Access Control — `BLOCKED`: prove managed-scope cron/state writes
  and no unknown-job mutation.
- A02 Security Misconfiguration — `BLOCKED`: verify fail-closed config,
  single-worker lock, exact installed package, and cron readback.
- A03 Software Supply Chain Failures — `BLOCKED`: dependency audit, locked
  dependencies, package/source hash parity.
- A04 Cryptographic Failures — `BLOCKED`: verify receipt/hash checks and ensure
  no secrets in logs or repository.
- A05 Injection — `BLOCKED`: prove message text cannot become a path, shell
  command, config field, or unsafe structured output.
- A06 Insecure Design — `BLOCKED`: prove durable-write-before-cursor,
  rich/core separation, idempotency, rollback, and duplicate handling.
- A07 Authentication Failures — `NOT_APPLICABLE_WITH_EVIDENCE`: this change
  does not alter Discord authentication; existing token handling remains
  external to the repository and will be verified redacted.
- A08 Software/Data Integrity Failures — `BLOCKED`: verify readback, hashes,
  monotonic cursors, trusted index receipt, and restore evidence.
- A09 Logging and Alerting Failures — `BLOCKED`: prove core and optional-rich
  status are distinguishable and failure cannot report green.
- A10 Exceptional Conditions — `BLOCKED`: test API error, partial write,
  stale inventory, restart/resume, duplicate input, and memory-safe execution.

## Release blocker

Any unresolved P0/P1 finding, unexplained data discrepancy, or unclassified
A01–A10 item blocks deployment and completion.
