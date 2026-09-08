# Daily Rich Recovery — OWASP Top 10:2025 Gate

Date: 2026-09-08

Release decision: `BLOCKED` until the live full baseline, root/compatibility
readback, incremental catch-up, and installed package verification pass.

ASVS v5.0.0: `NOT_APPLICABLE_WITH_EVIDENCE`. This is a local deterministic
CLI/Skill, not a web application or network service. Discord REST/CDN, local
filesystem authority, credentials, archive state, and business-logic safety
remain covered below.

## Security scope

- Sensitive data: private Discord content, authorship metadata, attachments,
  state cursors, and queue entries.
- Trust boundaries: installed config to local runner; Discord REST/CDN to the
  normalizer/downloader; staging generations to the archive-root selector;
  root selector to compatibility CURRENT pointers and the daily writer.
- Mutation boundary: local archive generations and pointers only. State and
  queue must remain byte- and inode-identical during baseline enrichment.
- Secrets: Discord token remains in the existing secure runtime source and is
  never written to argv, receipts, logs, archives, Git, or asset requests.

## Controls and evidence

- A01 Broken Access Control — `BLOCKED`: exact guild and exact 182 stable IDs,
  exhaustive active/archived public/private/joined-private inventory, and live
  per-entry reads are implemented. Awaiting live 182/182 baseline readback.
- A02 Security Misconfiguration — `BLOCKED`: absolute no-symlink roots,
  owner-private runtime files, canonical shared lock, strict bounds, and
  fail-closed missing-baseline behavior are implemented. Awaiting installed
  package/post-run evidence.
- A03 Software Supply Chain Failures — `BLOCKED`: deterministic package,
  runtime component hashes, source/package parity, dependency and secret scans
  are required. Awaiting final candidate hashes and clean reviewed commit.
- A04 Cryptographic Failures — `BLOCKED`: SHA-256 binds live evidence,
  generations, manifests, root pointer, receipts, state, and queue; Discord TLS
  remains certificate-verified and asset requests carry no Bot authorization.
  Awaiting live receipt verification.
- A05 Injection — `PASS`: content remains typed data; no shell/eval/template
  execution uses message fields. Canonical paths, Unicode normalization,
  Markdown escaping, hostile URL/filename tests, and source-census tests pass.
- A06 Insecure Design — `BLOCKED`: all entries seal before RUN_CURRENT,
  RUN_CURRENT is atomically read back before compatibility pointers, and daily
  sync globally blocks on any missing CURRENT. Awaiting live cutover and
  interruption/resume evidence.
- A07 Authentication Failures — `BLOCKED`: credential confinement, expected
  application capability, bounded authenticated REST, and credential-free CDN
  fetches are implemented. Awaiting live application capability preflight.
- A08 Software or Data Integrity Failures — `BLOCKED`: state/queue hashes and
  inode identity, archive-root identity, generation manifests, root manifest,
  checksummed pointers, exact readback, and private immutable pre-repair
  evidence are implemented. Awaiting live 182/182 verification.
- A09 Security Logging and Alerting Failures — `BLOCKED`: deterministic
  redacted receipts and existing 07:05 single health report are implemented.
  Awaiting successful recovery receipt and next natural health report.
- A10 Mishandling of Exceptional Conditions — `BLOCKED`: request/runtime/page/
  message/asset/disk limits, bounded 429 retries, terminal pagination, atomic
  writes, fail-closed exceptions, and resume-after-root logic are implemented.
  Interrupted materialized-stage recovery now requires fresh live equivalence,
  exact stage topology, verified attachment bytes, and unchanged base CURRENT;
  all other drift rejects before reservation or PASS evidence. Awaiting live
  fault-free completion and catch-up.

## Attacker-perspective release checks

- Wrong/missing/new entry, incomplete archived pagination, wrong guild,
  missing message-content capability, path traversal, symlink/hardlink,
  state/queue replacement, generation/manifest/pointer tamper, duplicate IDs,
  repeated/non-moving pages, SSRF/redirect, unknown visible fields, attachment
  truncation, rate-limit exhaustion, and root/compatibility partial completion
  must all fail closed.
- A scanner-only result cannot release this change. Final release requires the
  full test suite, deterministic package parity, installed post-run check,
  live 182/182 proof, queue/cursor verification, and manual diff review.

## Interrupted-stage candidate evidence

- Full repository suite: 445/445 PASS.
- Signed CDN query churn rebinds to fresh source evidence without a second
  download; semantic filename drift and tampered local bytes fail closed.
- Python compile, runtime-component hash binding, deterministic package/source
  parity, repository post-run check, and diff hygiene PASS.
- Release decision remains `BLOCKED` until the exact commit is installed and
  the live baseline, cursor catch-up, and natural schedule gates pass.

## Active metadata-probe budget candidate evidence

- The elapsed cap now measures only time spent inside actual CDN metadata
  probes; unrelated full-run enumeration, download, hashing, sealing, and idle
  time no longer consume it.
- The shared request cap and cumulative active-probe time cap remain fail
  closed. Per-request timeout, exact-host DNS/SSRF controls, redirect limits,
  credential-free requests, content-length validation, byte/file quotas, and
  disk reserve are unchanged.
- Full repository suite: 447/447 PASS, including long non-probe wall-time and
  cumulative active-probe cap regressions.
- Release decision remains `BLOCKED` pending exact-commit install, live
  baseline completion, cursor catch-up, and natural-schedule acceptance.

## Embed schema and interrupted-prefix candidate evidence

- Newly observed `embed.reference_id` and media `description` leaves are
  explicitly classified and remain present in deterministic structured
  Markdown. Unrecognized nested embed leaves still fail closed.
- Interrupted recovery accepts only base receipt plus a valid asset reservation
  and optional checksummed live evidence. Audit receipts, manifests, unsafe or
  unexplained files, dependency gaps, checksum drift, and attachment-byte
  mismatch are rejected before cleanup.
- Fresh Discord evidence rewrites canonical/raw records before local and prior
  evidence verification; only then are obsolete incomplete receipts removed
  and a new run-bound reservation permitted.
- Full repository suite 450/450 PASS, including six focused schema/recovery
  tests and a tampered reservation that leaves all evidence files untouched.
  Release remains `BLOCKED` pending exact install, live 182/182 baseline,
  catch-up, and natural run.
