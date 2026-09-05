# Deterministic Daily Rich Runner — OWASP Top 10:2025 Gate

Status: PASS for this branch scope; live deployment remains outside this gate.

## Security scope

- Data: private Discord messages, metadata, attachments, state, queue, and local
  archive receipts.
- Trust boundaries: Discord API to deterministic fetcher; fetched JSON to
  `RichArchiveStore`; verified generation to cursor/state persistence.
- Roles/tenants: one local operator, with exact guild/channel/entry binding.
- External systems/cost: Discord API only; requests and retries remain bounded.
- AI/tool capability: none at runtime. Daily sync is a fixed-argv command.

## Required evidence

- A01 Broken Access Control — exact channel ID from the selected state entry;
  path containment; excluded/invalid entries rejected; no cross-entry write.
- A02 Security Misconfiguration — fixed argv, private logs/receipts, no prompt
  fallback, owner-only no-follow shared lock, current complete inventory required.
- A03 Software Supply Chain — standard library runtime where practical;
  deterministic package build and dependency audit.
- A04 Cryptographic Failures — archive/store SHA-256 verification is required;
  no credentials or message data in receipts, logs, or Git.
- A05 Injection — no shell, `eval`, or content-derived paths/commands; message
  data is passed only to the rich archive API.
- A06 Insecure Design — archive generation must verify and become current before
  cursor advancement; missing API or ambiguous verification fails closed.
- A07 Authentication Failures — Bot token remains inside Discord API fetches and
  is never included in archive asset requests, stdout, receipts, or subprocess argv.
- A08 Integrity Failures — exact generation readback, atomic state/queue replace,
  cursor monotonicity, and failure-injection tests.
- A09 Logging/Alerting Failures — bounded structured receipt distinguishes safe
  lock/inventory skips from errors without leaking message bodies or paths.
- A10 Exceptional Conditions — lock contention, malformed inventory/state/queue,
  API absence, 403/429, aggregate wait exhaustion, oversized response, store
  failure, cap handoff, and persistence failure are explicit tests.

The rich archive downloader remains the single binary-fetch authority. Its exact
Discord CDN host allowlist, HTTPS-only redirects, DNS/IP validation, disabled
ambient proxy credentials, byte quotas, and hashes are prerequisites for the
daily runner's acceptance test; the runner never sends the Bot token to it.

## Business-logic abuse cases

- an LLM or prompt bypasses `RichArchiveStore` and writes raw Markdown;
- a stale slot overwrites a newer cursor;
- a rich archive failure advances state;
- a null-cursor or active-backlog entry is processed by the wrong owner;
- exactly-full pages are falsely marked caught up;
- two slots write concurrently after restart;
- an adopted look-alike job is changed without exact authorization.

## Applicability

- AI security overlay: not applicable — runtime invokes no model and grants no
  content-driven tools.
- ASVS 5.0.0: not applicable — local CLI/cron Skill, not a Web/API service.
- Human gate: live cron deployment remains with the parent transactional rollout
  and is not authorized by this isolated implementation branch.

## Recorded result

- A01 PASS — exact channel binding, safe relative entry roots, excluded/queued
  ownership, and symlink rejection are covered by runner and existing worker tests.
- A02 PASS — all three manifest slots are isolated commands, share the reviewed
  lock contract, and the daily prompt is absent from source and package.
- A03 PASS — runtime is standard-library Python; deterministic package parity and
  installed-layout smoke passed.
- A04 PASS — rich generation SHA-256, manifest verification, positive merge result,
  and CURRENT generation readback passed against the real core.
- A05 PASS — the daily runner invokes no shell/eval path and never derives paths or
  executable arguments from Discord message content.
- A06 PASS — injected merge failure preserves the cursor and queues retry; same-ID
  lookback refresh does not advance the new-message cursor.
- A07 PASS — Bot authorization is scoped to Discord API requests; the independently
  constructed asset downloader receives no token or ambient proxy credentials.
- A08 PASS — atomic state/queue persistence, verified generation publication, exact
  readback, and two-process lock serialization passed.
- A09 PASS — managed receipts distinguish real error, inventory warning, and safe
  lock skip; public output contains only bounded counts/categories.
- A10 PASS — response-byte cap, aggregate 429 wait cap, lock contention, merge
  failure, malformed contract, and package/install smoke are covered.

Evidence:

- `python3 -m pytest tests`: 267 PASS, including the real two-process lock canary
  and shared rich asset-probe budget coverage.
- targeted runner/core/cron/managed/package set: 97 PASS.
- `post_run_check.py`: all repository, package-parity, manifest, restore, and
  immutable-evidence checks PASS.
- `git diff --check`, all-script `py_compile`, secret-signature scan, manifest
  validation, and required/forbidden package file contract all PASS.
