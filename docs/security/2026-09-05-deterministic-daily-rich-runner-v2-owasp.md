# Deterministic Daily Rich Runner v2 — OWASP Top 10:2025 Gate

Status: implementation plan; release decision is BLOCK until final tests and an
independent review close every item.

## SECURITY_SCOPE

- Data classification: private Discord messages and metadata; private local
  archive/state/queue/evidence; secret Discord Bot credential.
- Trust boundaries: Discord API response to normalizer; inventory report to
  entry selection; runner to rich-core mutation API; `CURRENT` generation to
  state cursor; config/state/queue to filesystem.
- Roles and tenants: one configured guild, one local OS owner, stable
  channel/thread identities; no cross-guild or cross-entry write is allowed.
- External services and costs: bounded Discord API and allowlisted Discord CDN
  traffic; local disk capacity and runtime.
- AI tools and write capabilities: no model at runtime.  The managed command may
  write versioned archive generations, queue, state, and redacted receipts only.
- Maximum damage: silent cursor advancement past missing content, mixed entry
  data, unbounded network/disk cost, credential exposure, or corrupted CURRENT.

## THREAT_MODEL

Attackers and failures include malicious Discord content, malformed/oversized API
responses, stale or forged inventory, hostile symlinks, concurrent jobs, forged
lock proof, incomplete pagination, crash between publication and cursor write,
asset amplification, and a compromised/incorrect rich-core contract.

The runner treats every external payload and local mutable JSON file as
untrusted.  It executes no content, follows no content-derived command/path, and
fails closed when ownership, evidence, pagination, or readback is ambiguous.

## BUSINESS_LOGIC_ABUSE_CASES

- fabricated `ok`/`verified` merge output advances a cursor;
- a forged token or per-entry lock permits two slots to publish concurrently;
- per-entry asset budgets reset and exceed the slot-wide cap;
- a quiet entry with no rich baseline is labelled healthy;
- a full page is mistaken for a terminal page;
- old edits/reactions/pins are never revisited;
- active backlog and daily sync both own the same entry;
- a crash publishes CURRENT but loses retry/state ownership;
- one channel's source hashes are accepted for another channel;
- output/logging exposes Bot tokens, message content, URLs, or private paths.

## OWASP_2025_PLAN

- A01 Broken Access Control — BLOCK pending tests for exact guild/channel/entry
  identity, path containment, exclusion, active-queue ownership, and wrong-token
  rejection.
- A02 Security Misconfiguration — BLOCK pending fixed command topology, supported
  contract-version gate, owner-only files/lock, and no prompt fallback.
- A03 Software Supply Chain Failures — BLOCK pending deterministic package build,
  source/package parity, compile, dependency inventory, and secret scan.
- A04 Cryptographic Failures — BLOCK pending recomputed inventory, generation,
  pointer, source-record, and operation-receipt SHA-256 checks.  Credentials must
  remain outside argv/output/archive assets.
- A05 Injection — BLOCK pending negative tests for path traversal, symlinks,
  control characters, hostile Markdown/JSON/URLs, and proof that no shell/eval or
  content-derived executable path exists.
- A06 Insecure Design — BLOCK pending exact merge/readback state machine, one
  shared token/budget, mutable refresh, quiet-baseline gate, and race tests.
- A07 Authentication Failures — BLOCK pending proof that the Bot credential is
  used only for Discord API authorization and never forwarded to CDN downloads,
  receipts, logs, or subprocess argv.
- A08 Software/Data Integrity Failures — BLOCK pending CURRENT/canonical/queue/
  state crash injection, exact ID/hash readback, journal recovery, package parity,
  and rollback/replay proof.
- A09 Security Logging and Alerting Failures — BLOCK pending fixed typed reasons,
  redacted bounded receipts, and tests separating safe lock skip from real errors.
- A10 Mishandling Exceptional Conditions — BLOCK pending malformed/403/429/
  oversized/truncated response, exact-full page, merge/readback failure, disk/
  quota, persistence failure, cancellation, and restart/resume tests.

## AI_SECURITY_OVERLAY

`NOT_APPLICABLE_WITH_EVIDENCE` for runtime model execution: all three daily jobs
are fixed deterministic commands and no model receives Discord content or tools.
Skill/package installation remains supply-chain scoped under A03 and requires
review before live deployment.

## ASVS_5_0_0_REQUIREMENT_REGISTER

Target: `not_applicable_with_reason`.  This is a local CLI/cron data pipeline, not
a Web/API server.  Equivalent controls are the filesystem ownership, network
egress, deterministic state-machine, evidence, and fault-injection requirements
in this document.  Reviewer confirmation is required before PASS.

## HUMAN_SECURITY_GATES

- No push, merge, live cron mutation, live data rebuild, or production deployment
  from this isolated branch.
- Parent transactional rollout must snapshot and verify rollback inputs, disable
  old jobs, install exact package bytes, read back all owned jobs, run canaries,
  then enable.
- Any P0/P1, unaccepted integrity P2, incomplete full suite, or package drift
  keeps release BLOCKED.

## Planned evidence

- targeted daily/core/topology/managed/package tests;
- full pytest and Python compile logs;
- source/package byte-parity and manifest verification;
- secret and unsafe-pattern scan;
- negative/fault-injection test names mapped to A01–A10;
- independent review of exact commits;
- clean Git status and commit hashes.

## SECURITY_CLOSEOUT

- OWASP_A01_A10_STATUS: all BLOCKED pending implementation evidence.
- ASVS_OR_EQUIVALENT_SCOPE: local pipeline controls above; reviewer pending.
- BUSINESS_LOGIC_NEGATIVE_TESTS: pending.
- AI_SECURITY_OVERLAY: runtime N/A with boundary evidence; reviewer pending.
- SAST_DAST_DEPENDENCY_SECRET_SCAN: pending.
- THREAT_MODEL_REVIEW: pending.
- OPEN_P0_P1_P2_P3: pending independent review.
- ACCEPTED_RESIDUAL_RISK_OWNER: none.
- EVIDENCE_PATHS: pending.
- COMMIT: pending.
- RELEASE_DECISION: BLOCK.
