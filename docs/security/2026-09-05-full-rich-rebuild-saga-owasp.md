# Full Rich Discord Archive Rebuild Saga — OWASP Top 10:2025 Security Plan

Date: 2026-09-05

Release decision: `BLOCK`

Reason: this is the pre-implementation security contract. Every in-scope control remains `BLOCKED` until implementation, reproducible evidence, and independent review exist. A green scanner alone cannot release the live cutover.

Companion task: `docs/tasks/2026-09-05-full-rich-rebuild-saga.md`

ASVS v5.0.0 target: `NOT_APPLICABLE_WITH_EVIDENCE`. This repository delivers a local deterministic CLI/Skill and does not expose a web application or network API. Equivalent controls are the A01-A10 matrix, filesystem/network threat model, business-logic negative tests, and attacker-perspective review below.

## SECURITY_SCOPE

- Data classification: private Discord messages, authorship metadata, membership-visible thread inventory, attachments, local-only/deleted legacy content, state/queue cursors, and local filesystem topology are sensitive customer data. Source, tests, and redacted schemas are public-safe. Discord credentials and runtime capability nonces are secrets and may not enter archives, receipts, logs, Git, or chat.
- Assets: the current selected run, 178 entry generations, immutable pre-repair evidence, state/queue bytes, run/entry manifests, attachment bytes/hashes, root and compatibility pointers, transaction journals, disk reservation, and restore evidence.
- Trust boundaries: installed config/state/queue -> preflight; Discord REST -> bounded collector; Discord CDN -> safe downloader; untrusted message/asset payload -> normalizer/renderer; legacy archive -> retained projection; staging -> verified generation; 178 entry generations -> run manifest -> `RUN_CURRENT.json`; persisted evidence -> runtime-only verifier.
- Roles and tenants: one authorized local operator and one configured Discord application/guild. Every channel/thread is an isolation boundary. No cross-guild, cross-entry, or unapproved private-thread access is permitted.
- External services and cost: Discord REST and Discord CDN only. Requests, retries, sleep, response bytes, pages, files, asset bytes, runtime, redirects, and disk consumption are globally bounded by one run context.
- AI tools and write capabilities: the archive path is deterministic and must not invoke a model. Message content is data only. Live pointer replacement is a high-impact local write covered by existing direct-repair authorization; permission changes, deletion, external publication, and push/deploy remain separate gates.
- Production boundary: live archive root, installed state/queue, current cron writers, and Qwen/LanceDB readers. The run must quiesce writers, keep state/queue byte-identical, and select a new run only after full runtime verification.

## THREAT_MODEL

Potential attackers and failures:

- A malicious Discord user controlling message text, Markdown, Unicode, component/embed fields, URLs, filenames, payload size, and update timing.
- A compromised or misconfigured Discord endpoint/CDN response, redirect target, proxy environment, or expired signed URL.
- A local unprivileged process racing paths, symlinks, hardlinks, locks, pointers, reservation files, state/queue, or receipts.
- An operator or buggy adapter supplying a wrong root, stale baseline, partial inventory, fabricated receipt, reset budget, or unsafe resume input.
- Crashes, cancellation, disk exhaustion, 429/5xx storms, permission changes, persistent Discord activity, and partial fsync/rename/readback.

Highest plausible impact:

- A false-green archive that silently omits visible private conversation or attachments.
- Cross-entry writes or replacement of the selected archive with a mixed/corrupt generation.
- Credential leakage to attachment hosts, logs, receipts, Git, or Discord output.
- SSRF to local/private/metadata endpoints.
- Loss of recoverable legacy history or cursor advancement beyond durable archive data.
- Unbounded API/disk use or a rebuild that never converges because its own reports create messages.

Security invariants:

- No persisted file can self-authorize live PASS.
- No entry or root pointer changes before exact runtime completeness verification.
- No state/queue byte changes during full rebuild/enrichment.
- No legacy deletion.
- No Bot Authorization/cookies/proxy credentials on asset requests.
- No entry worker owns independent global quotas, disk reservations, or lock authority.
- Any unknown, truncated, stale, permission-denied, or unclassified condition fails closed.

## BUSINESS_LOGIC_ABUSE_CASES

- Supply an archive root or baseline through traversal, Unicode controls, symlink, hardlink, mount/inode swap, or workspace overlap.
- Add/remove a thread between inventory and cutover so an apparent 178/178 result covers the wrong set.
- Hide an archived private/joined-private thread behind incomplete pagination or missing membership.
- Disable `MESSAGE_CONTENT` so visible messages become false empties.
- Forge stored `PASS`, 100% percentages, cutoff, inventory digest, terminal page, or runtime token fields.
- Reset request/asset budgets per entry or after resume to multiply cost by 178.
- Replay a stale lock/evidence capability after lock release, fork, process restart, TTL expiry, or first use.
- Race state/queue/pointers while the rebuild releases the lock for maintenance.
- Keep convergence alive with report messages, or suppress ordinary user messages using loose author/content heuristics.
- Reuse a signed asset URL for a different message/asset, redirect to private infrastructure, or leak the Discord Bot token.
- Use equal timestamps/hash ordering to let a stale same-ID revision replace current truth.
- Crash at pointer/journal/fsync boundaries and expose a mixed generation.
- Delete duplicate/local-only/parser-unknown legacy data as "cleanup" before restore proof.

## OWASP_2025_MATRIX

### A01:2025 Broken Access Control — BLOCKED

Planned controls:

- Exact guild/application identity and exact 178-entry stable-ID/path binding.
- Complete active and archived public/private/joined-private inventory with terminal-page proof.
- Effective `VIEW_CHANNEL`, `READ_MESSAGE_HISTORY`, private-thread membership, and `MESSAGE_CONTENT` proof.
- Path containment, original-component symlink rejection, single-link regular files, expected owner/mode, archive-root device/inode binding.
- Module-issued lock/evidence capabilities bound to store/run/lease/process and impossible to deserialize.

Required tests/evidence:

- 177/178, 179/178, wrong guild, duplicate channel/path, missing private thread, 403, permission drift, and content-redaction negative tests.
- Cross-entry channel mismatch and path traversal/symlink/hardlink/inode-swap races.
- Stale/foreign/forked/expired/replayed capability rejection.
- Private runtime receipt showing 178/178 and all endpoint classes complete.

Owner: implementer; independent reviewer verifies. Status becomes PASS only after live readback.

### A02:2025 Security Misconfiguration — BLOCKED

Planned controls:

- Explicit expected archive/baseline/state/queue roots; no filesystem auto-discovery.
- Private 0700 directories and 0600 regular files, no ambient proxy/netrc/cookies, fixed TLS hosts/ports, bounded defaults.
- Effective privileged intent and per-entry permission preflight.
- Quiesced managed writers and configuration/cron drift detection on every resume.
- Persisted receipts are `AUDIT_ONLY`; runtime PASS is non-serializable and short-lived.

Required tests/evidence:

- Wrong root/config, overlapping roots, permissive modes, symlink components, proxy environment, stale inventory/baseline, missing intent, cron de-quiescence, and stored-PASS rejection.
- Post-cutover configuration/readback smoke.

Owner: implementer/reviewer. Status remains BLOCKED until exact package and live configuration evidence exists.

### A03:2025 Software Supply Chain Failures — BLOCKED

Planned controls:

- Reviewed source hash binds runtime evidence and the root receipt.
- Fixed executable/network adapter identities, deterministic package, source/dist byte parity, no unreviewed runtime dependency.
- Dependency/license/secret scan and package manifest verification.
- Unknown Skill/plugin/script cannot obtain the archive lock token or runtime evidence capability.

Required tests/evidence:

- Source/dist 100% parity, deterministic `.skill`, Python compile, dependency audit, secret scan, manifest/post-run check, and independent diff review.
- Modified adapter/package hash causes resume/runtime gate failure.

Owner: release reviewer. Status remains BLOCKED until final integration commit is reviewed.

### A04:2025 Cryptographic Failures — BLOCKED

Planned controls:

- Modern TLS with certificate verification for Discord REST/CDN; no insecure fallback.
- SHA-256 for canonical payloads, visible payloads, files, evidence, generations, run manifest, pointers, and receipts.
- Cryptographically random runtime nonces; persisted records store only nonce digests and binding metadata.
- Expiring signed URL query parameters are observations, excluded from stable identity but preserved safely.
- Secrets never enter command previews, archives, receipts, logs, Git, or asset requests.

Required tests/evidence:

- TLS/certificate failure, tampered hash/manifest/evidence, nonce replay/expiry, signed-query rotation, and credential-absence captures.
- Secret-pattern scan over source, dist, generated receipts, logs, and archive fixtures.

Residual note: SHA-256 detects accidental/tampered local bytes but is not claimed as an external authenticity signature.

Owner: implementer/reviewer.

### A05:2025 Injection — BLOCKED

Planned controls:

- Message text, components, embeds, polls, snapshots, filenames, URLs, and receipts remain typed data and never enter shell/eval/template execution.
- Fixed argv with `shell=False`; canonical path construction, NFKC/case-fold collision checks, all Unicode control rejection, and context-aware Markdown escaping/inert remote media.
- Strict JSON schemas, sizes, enums, and unknown-visible-field census fail closed.

Required tests/evidence:

- Shell/template/Markdown/HTML/header/path payloads, bidi/control characters, hostile filenames, duplicate Unicode names, malformed JSON, unknown future visible roots, and mutation tests that remove nested visible fields.
- Static contract proving no prompt/direct raw writer controls the rebuild.

Owner: implementer/reviewer.

### A06:2025 Insecure Design — BLOCKED

Planned controls:

- Per-entry immutable generations plus one authoritative run manifest and atomic root selector.
- Resumable monotonic saga, receipt-first transitions, state/queue invariance, two zero rounds, and exact self-drift IDs only.
- Single `FullRebuildRunContext` owns global quotas, deadline, disk reservation, lock lease, and evidence capabilities.
- Independent exact-set/count/fingerprint verification; caller percentages are non-authoritative.
- No historical-completeness claim beyond current API-visible truth and retained local provenance.

Required tests/evidence:

- Fabricated 100%/PASS/empty evidence; partial inventory; budget reset; new messages during every phase; same-ID mutable updates; equal-timestamp conflict; self-drift heuristic misuse.
- Root selection proves only old or complete new run is visible at every crash point.
- Attacker-perspective review after all scanners pass.

Owner: architecture reviewer and PM. Any unresolved completeness P2 is release-blocking.

### A07:2025 Authentication Failures — BLOCKED

Applicability: the Skill does not implement an authentication endpoint, but it uses a Discord Bot credential and runtime capabilities; credential confinement and application identity are in scope.

Planned controls:

- Credential loaded only through the existing secure runtime path, never persisted by the rebuild.
- Discord API requests bind to the expected application/guild; CDN requests contain no Bot Authorization, cookies, or ambient credentials.
- Runtime capabilities are process-bound, TTL-limited, one-shot, operation-bound, and invalid after fork/restart/lock release.

Required tests/evidence:

- Wrong bot/application/guild, missing credential, token in URL/log/receipt, CDN/redirect authorization leakage, capability construction/replay/fork/expiry.

Owner: implementer/reviewer. It may become `PASS`, not N/A, because credentials are actively used.

### A08:2025 Software or Data Integrity Failures — BLOCKED

Planned controls:

- Immutable baseline and legacy manifests, canonical/source/visible/asset hashes, generation manifests, run binding, atomic no-follow writes, directory fsync, exact pointer readback.
- Stored evidence recomputed independently; live authority exists only in a current collector capability.
- Resume verifies every sealed generation and all state/queue/root identities before reuse.
- Post-cutover reader/indexer canary and checked pointer rollback.

Required tests/evidence:

- Tamper any message, pointer, generation, asset, baseline, entry receipt, root receipt, inventory, state/queue, or journal.
- Fault injection at every write/fsync/rename/readback boundary; restore canary selects exact prior bytes on failure.
- Legacy evidence re-verifies after success and rollback.

Owner: implementer/reviewer.

### A09:2025 Security Logging and Alerting Failures — BLOCKED

Planned controls:

- Private bounded receipts contain counts, hashes, phase, reason enums, correlation/run IDs, lease epochs, retries, errors, and recovery actions without message bodies, credentials, exact sensitive paths, or unnecessary personal data.
- Report `PAUSED`, `FAILED`, `AUDIT_ONLY`, and transient runtime `PASS` distinctly.
- Actionable alerts identify permission, inventory, disk, asset, convergence, tamper, and rollback uncertainty.
- Rebuild runner is silent in Discord until terminal state; self-report deferral uses exact outbound IDs.

Required tests/evidence:

- Redaction tests, oversize-log bounds, forged status rejection, alert canary for at least one failing gate, and report-self-drift negative cases.
- Evidence retention/access/mode check.

Owner: implementer/operations reviewer.

### A10:2025 Mishandling of Exceptional Conditions — BLOCKED

Planned controls:

- Bounded 429/5xx/transport retry, global deadline/cancellation, pagination stall checks, asset refresh, real disk reservation, and fail-closed unknowns.
- Durable idempotent saga and lock-reacquisition proof; stale tokens cannot resume writes.
- Root pointer rollback uses prior exact bytes and preserves failed stages.
- No cleanup deletes legacy/staged evidence on uncertain failure.

Required tests/evidence:

- 429 retry-after, 5xx storms, malformed/truncated/oversized bodies, 400/401/403/404, signed URL expiry, redirect loop, network cancellation, disk full/pressure, process kill, lock contention, state/queue drift, and readback false-success.
- Kill/restart and fault injection before/after every journal/fsync/entry-seal/root-pointer phase.
- Two zero rounds under continuous activity and safe `PAUSED` when convergence deadline expires.

Owner: implementer/reviewer.

## Network and SSRF verification

The downloader must reject:

- non-HTTPS, non-443, userinfo, IP literals, loopback, private, link-local, multicast, reserved, metadata, and ambiguous/IDN-confusable targets;
- DNS rebinding or a redirect whose resolved IP/host is outside the exact Discord CDN allowlist;
- environment proxies, netrc, cookies, cross-host credential forwarding, redirect loops, non-identity encodings, oversized/truncated bodies, and non-200 terminal responses.

Every redirect hop is revalidated. Test instrumentation must prove the Discord Bot Authorization header and all cookies are absent from initial and redirected asset requests.

## Global resource-abuse verification

- Quotas are owned once per run and monotonically consumed across all 178 entries, retries, resumes, and asset refreshes.
- Tests must prove entry workers cannot instantiate/reset a context or substitute a larger child quota.
- Disk reservation must allocate real blocks, preserve a free-space floor, and reconcile after crash/resume.
- API/request/sleep/page/file/byte/runtime exhaustion produces `PAUSED` or `FAILED`, never partial PASS.
- No automatic recurring full-history rebuild is added; live execution remains an explicitly authorized maintenance transaction.

## AI_SECURITY_OVERLAY

Status: `NOT_APPLICABLE_WITH_EVIDENCE` for the rebuild implementation.

Reason: the coordinator, collector, normalizer, renderer, verifier, and transaction logic are deterministic and invoke no model. Discord message content cannot select tools, commands, filesystem roots, network hosts, permissions, or release decisions. If any future wrapper lets an LLM derive arguments or state transitions from archived content, this status becomes invalid and the full AI Security Overlay is required.

Verification:

- Static contract rejects prompt/LLM execution in the rebuild path.
- Fixed schemas and argv validate every external input before action.
- Human authorization remains required for permission changes, deletion, publication, and deployment outside the approved local repair.

## ASVS_5_0_0_REQUIREMENT_REGISTER

- Target: `NOT_APPLICABLE_WITH_EVIDENCE`.
- In-scope count: 0 Web/API requirements; this project exposes no HTTP application/API endpoint.
- Equivalent verification owners: implementer for CLI/filesystem/network controls; independent reviewer for OWASP matrix and threat model; PM/operator for live cutover evidence.
- Reconsideration trigger: adding a daemon, HTTP health/admin endpoint, remote upload, webhook, multi-user service, or browser interface.

## HUMAN_SECURITY_GATES

Existing direct-repair authorization permits staged local rebuild and a non-destructive atomic selection only after every gate passes. Separate explicit approval is still required for:

- changing Discord guild/application permissions or privileged intents;
- deleting or compacting legacy/evidence/staged generations;
- weakening the exact 178/inventory/coverage/asset/zero-round gates;
- adding a cloud fallback or external data transfer;
- pushing/merging/releasing an unreviewed package or changing unrelated live services.

## Required security closeout

The final closeout must provide:

```text
SECURITY_CLOSEOUT:
- OWASP_A01_A10_STATUS: A01..A10 each PASS|BLOCKED|NOT_APPLICABLE_WITH_EVIDENCE
- ASVS_OR_EQUIVALENT_SCOPE: local CLI/Skill threat model + OWASP matrix
- ASVS_5_0_0_REGISTER_COUNTS: 0 / 0 / 0 / 0 with N/A boundary evidence
- BUSINESS_LOGIC_NEGATIVE_TESTS: command/evidence paths and pass counts
- AI_SECURITY_OVERLAY: NOT_APPLICABLE_WITH_EVIDENCE + static contract evidence
- SAST_DAST_DEPENDENCY_SECRET_SCAN: exact tool/evidence references
- THREAT_MODEL_REVIEW: independent reviewer and commit
- OPEN_P0_P1_P2_P3: exact counts and accepted residual-risk owner
- LIVE_SCOPE: exact 178 inventory digest, permission evidence, state/queue invariant
- RESTORE_ROLLBACK: prior pointer bytes/hash + reader/indexer canary
- EVIDENCE_PATHS: private redacted references only
- COMMIT: reviewed integration commit
- RELEASE_DECISION: PASS|BLOCK|HUMAN_GATE
```

Release is `BLOCK` when any A01-A10 item lacks evidence; any P0/P1 is open; a P2 can affect completeness, confidentiality, credential confinement, cross-entry isolation, resource exhaustion, or atomic recovery; runtime inventory/capability evidence is stale; state/queue changed; or the final reader/indexer canary has not passed.

## Minimum final evidence set

- Targeted and complete test logs, including all crash/fault-injection cases.
- Exact fresh 178/178 active/archived public/private/joined-private inventory receipt.
- Effective permission and `MESSAGE_CONTENT` capability receipt.
- Immutable baseline and legacy-retained verification.
- Global budget/disk-reservation accounting and signed-URL/SSRF tests.
- Per-entry receipts and root binding with four 100% coverage dimensions and zero errors.
- Baseline, delta, and two fresh zero-round evidence sets.
- State/queue byte-identity proof and old/new root-pointer exact bytes/hashes.
- Post-cutover exact readback, isolated reader/indexer canary, and checked rollback rehearsal.
- Deterministic source/dist package parity, dependency/secret scans, and fresh independent security review.
