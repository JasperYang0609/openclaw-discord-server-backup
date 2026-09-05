# Deterministic Daily Rich Runner — OWASP Top 10:2025 Gate

Status: implementation evidence pending.

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
  fallback, shared lock, current complete inventory required.
- A03 Software Supply Chain — standard library runtime where practical;
  deterministic package build and dependency audit.
- A04 Cryptographic Failures — archive/store SHA-256 verification is required;
  no credentials or message data in receipts, logs, or Git.
- A05 Injection — no shell, `eval`, or content-derived paths/commands; message
  data is passed only to the rich archive API.
- A06 Insecure Design — archive generation must verify and become current before
  cursor advancement; missing API or ambiguous verification fails closed.
- A07 Authentication Failures — Bot token remains inside Discord API fetches and
  is never included in archive assets, stdout, receipts, or subprocess argv.
- A08 Integrity Failures — exact generation readback, atomic state/queue replace,
  cursor monotonicity, and failure-injection tests.
- A09 Logging/Alerting Failures — bounded structured receipt distinguishes safe
  lock/inventory skips from errors without leaking message bodies or paths.
- A10 Exceptional Conditions — lock contention, malformed inventory/state/queue,
  API absence, 403/429, store failure, cap handoff, and persistence failure are
  explicit tests.

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
