# Rich Discord Message Archive Completeness

Status: approved for implementation by Jasper's direct repair request on 2026-09-05.

## Problem

The current archive proves that Discord message IDs were seen, but it does not prove that the visible conversation was preserved. `run_backlog_worker_v3.py` writes only `content` plus attachment names and remote URLs. Component-only messages, embeds, polls, stickers, forwarded snapshots, reply context, edit metadata, reactions, and attachment bytes can therefore be lost while the queue still reports `caught_up`.

The repair must preserve both searchable human-readable Markdown and a deterministic machine-verifiable source record. It must not destroy older local-only or legacy records while rebuilding current live history.

## Scope

In scope:

- A versioned canonical JSONL record for every live Discord message.
- Human-readable Markdown rendering of all supported visible payloads.
- Local attachment byte preservation with SHA-256 and explicit failure receipts.
- Full-history rebuild for every non-excluded state entry.
- Atomic per-entry cutover with immutable pre-repair evidence.
- Future backlog writes using the same canonical normalizer and renderer.
- All writers, including the three daily-sync slots, backlog, reconcile, and
  weekly refresh, using one deterministic archive API. No LLM or prompt may
  directly append to `raw/*.md` or advance archive state.
- Two independent completion gates: message-ID coverage and visible-payload fingerprint coverage.
- Fail-closed handling for pagination stalls, truncated responses, unknown visible structures, attachment errors, and crash-before-cutover.

Out of scope:

- Deleting legacy or duplicate history.
- Guessing content for Discord messages already deleted before the repair and absent from every local copy.
- Mutating Discord messages or server configuration.
- Changing the summary generation model or publishing private archive data externally.

## Storage contract

Each entry keeps its existing directory. A generation is written under
`generations/<generation-id>/`; a checksummed `CURRENT.json` pointer selects the
only readable generation. Every reader and the LanceDB indexer must resolve this
pointer rather than mixing separately renamed trees. Each generation contains:

- `canonical/YYYY-MM-DD.jsonl`: one sorted JSON object per message.
- `raw/YYYY-MM-DD.md`: deterministic readable rendering from the canonical record.
- `attachments/<message-id>/<safe-filename>`: downloaded bytes.
- `receipts/rich-archive-latest.json`: entry-level verification and attachment status.

Before the first rich rebuild, prior raw, canonical, attachment, receipt,
state, and queue data are copied into a checksummed, read-only evidence snapshot
outside the live archive tree. Rebuild output is created as a resumable staged
generation, verified, fsynced, and published by one atomic `CURRENT.json`
replacement. A durable transaction journal and startup recovery guarantee that
readers observe either the complete old generation or the complete new
generation. No unverified output replaces the selected generation.

Canonical JSONL records contain:

- schema version, message ID, channel ID, timestamps, type, flags, pin/TTS state;
- author identity fields returned by Discord;
- text content and mentions;
- recursively normalized components;
- embeds, poll, sticker items, forwarded/message snapshots, reply/reference context;
- interaction/system metadata and reactions;
- attachment metadata, original URL, local relative path, byte length, and SHA-256;
- a bounded, lossless, sanitized API source payload and its SHA-256;
- `sourcePayloadSha256` over the stable normalized source subset; expiring CDN
  signature query parameters are retained only as observations and excluded
  from revision identity;
- `visiblePayloadSha256` over the normalized human-visible subset;
- explicit `unknownVisibleFields` and `attachmentErrors` arrays.

Each message retains immutable `contentRevisions` and mutable `observations`.
Content revisions are keyed by edited timestamp plus stable source hash; a stale
revision cannot replace a newer one. Reactions, pin state, poll counts, embed
refreshes, and signed-URL observations may change without `edited_timestamp`, so
they are versioned independently by observation time and stable hash.

Records and Markdown are UTF-8 and sorted by numeric snowflake ID. JSON keys are stable and sorted. URLs and message text are data, never executed.

## Visible rendering

Every Markdown message block keeps the existing searchable ID header and renders labeled sections only when present:

- `[文字內容]`
- `[元件內容]`
- `[Embed]`
- `[投票]`
- `[貼圖]`
- `[轉寄快照]`
- `[附件]`
- `[回覆關係]`
- `[互動／系統資訊]`
- `[反應／編輯狀態]`

`(無文字內容)` may be used only when an independent source-field census proves
the normalized visible payload is truly empty. A component-only or embed-only
message must never render as empty. Message text is escaped as data: it cannot
forge a machine header, execute raw HTML, inline-load remote media, or alter the
coverage parser. Coverage trusts canonical IDs and renderer hashes, never regex
matches in message text.

## Attachment policy

- Inventory assets recursively from top-level and nested attachments, stickers,
  embed media, Components V2 media/file/gallery nodes, and message snapshots.
- Download Discord-hosted assets only from an exact configured Discord CDN host
  allowlist on port 443. Reject userinfo, IP literals, loopback/private/link-local
  targets, environment proxies, cookies/netrc, cross-host redirects, redirect
  loops, non-200 status, and non-identity encodings. Revalidate every redirect
  hop and never attach the Discord Bot Authorization header.
- External embed media remains explicit metadata-only and is excluded from the
  in-scope Discord binary denominator; it may never be silently counted as
  preserved bytes.
- Use attachment or asset ID (or a stable JSON pointer plus asset ID) as the
  storage identity. Filename is display-only after sanitization, so duplicate,
  case-folded, and Unicode-equivalent names cannot collide.
- Stream to a temporary regular file, hash while writing, verify declared size when available, then atomically rename. If both exact Discord attachment URL variants independently return bounded `200` responses whose current `Content-Length` differs from the historical payload size, preserve that payload size as source evidence, preflight the two observed sizes, retry both variants independently, and record the verified current sizes plus mismatch provenance. Never substitute a transformed proxy representation for an original representation in this case.
- The non-persistable live-evidence token remains PID／run／entry／generation bound and one-shot. For a full baseline, its expiry is the remaining bounded run deadline (maximum 24 hours), so a large attachment set cannot expire a fixed five-minute token during verified local materialization; exceeding the run deadline still fails closed before PASS evidence can be installed.
- Reuse an existing local file only when its stored SHA-256 and byte length verify.
- A missing, expired, oversized, truncated, or hash-mismatched attachment is recorded and makes the relevant entry and full run non-PASS. It must not be silently ignored.
- Remote URLs remain metadata, but a URL alone does not satisfy attachment completeness.
- Apply per-file, per-message, per-entry, and full-run file/byte quotas. Before
  downloading, compare total declared size plus reserve against free disk and
  fail before mutation if capacity is insufficient. Use private mode 0600,
  no-follow/exclusive file creation, path containment, and single-link checks.

## Rebuild transaction

1. Acquire the existing shared backup lock before any mutation.
2. Require a fresh deterministic Discord inventory with `complete=true`, full
   active and archived thread enumeration, exact state/inventory stable-ID set,
   and unique channel IDs and Unicode/case-folded relative paths.
3. Freeze state/queue and every pre-repair archive tree into immutable SHA-256 evidence.
4. Capture a high-watermark message ID independently for every entry. Fetch every
   page only through that cutoff. Messages newer than the cutoff belong to the
   next incremental run. Reject duplicate/stalled pagination and any live error.
5. Normalize each message, download/verify recursive assets, and write a durable
   per-entry staged receipt. Verified staged entries can resume after restart,
   but can never auto-publish without the full gate.
6. Validate per entry:
   - live IDs equal canonical IDs;
   - exactly one canonical record per live ID;
   - every canonical source and visible fingerprint recomputes;
   - Markdown contains exactly one block for each canonical ID;
   - every attachment referenced as complete exists and hashes correctly;
   - an independent JSON-pointer census of the lossless source accounts for every
     supported and unknown visible field, including message flags;
   - recursive in-scope Discord asset inventory equals verified local assets;
   - no unknown visible field is unaccounted for.
7. Publish one fully validated generation with an atomic pointer transaction.
   A full-run receipt remains FAIL unless every selected entry passes.
8. Enrichment/refresh runs do not advance cursors. Cursor updates are reserved
   for successfully committed new-ID writes through the verified cutoff.
9. Verify immutable evidence after cutover and retain it for rollback/review.

## Future incremental writes

`append_batch` and every other writer are replaced by one idempotent
`RichArchiveStore.merge_messages()` transaction. The three daily-sync cron jobs
invoke a deterministic command runner; their prompt fallback is removed and a
missing runner fails closed. A static contract test rejects any other code or
prompt that directly appends `raw/*.md`.

- acquire the shared lock;
- load the day's canonical records;
- normalize incoming messages and merge by ID;
- retain content revisions and mutable observations for the same ID using the
  independent version rules above;
- write staged JSONL and regenerate Markdown atomically;
- only after both files and attachments verify may state/queue cursors advance.

This prevents duplicate append noise. Incremental runs fetch new IDs; a bounded
lookback refresh and the weekly full refresh revisit existing IDs so edits,
reactions, pin state, poll counts, embeds, and asset URLs do not remain stale.

## Verification and completion gate

The repair is complete only when a post-cutover live closeout reports all of the following:

- `liveErrors = 0`
- `inventoryCoverage = 100%` against a fresh complete inventory digest
- `idCoverage = 100%`
- `visibleTextCoverage = 100%` using an independent source-field census
- `markdownCoverage = 100%`
- `binaryAssetCoverage = 100%` for recursively inventoried Discord-hosted assets
- `duplicateCanonicalIds = 0`
- `unknownVisibleFields = 0`
- `attachmentErrors = 0`
- immutable pre-repair evidence verification PASS
- crash/restart, 403/404, 429, pagination-stall, unknown-type, duplicate, and edit tests PASS
- every entry has a verified cutoff and a committed generation hash

Legacy duplicate IDs are retained in evidence and reported separately. Local-only
or Discord-deleted records also remain in a searchable `legacy-retained`
projection with provenance, but are excluded from the live coverage denominator.
They are not deleted during this change.

## Required tests

- Plain text and empty system message.
- Component-only type 17/10 message with nested text, button labels, select labels, placeholder, and URL.
- Embed-only message including title, description, fields, author/footer, and media metadata.
- Poll, sticker, forwarded/message snapshot, reply reference, interaction/system metadata, reactions, pin, and edited timestamp.
- Unicode, Markdown metacharacters, multiline content, and hostile path-like filenames.
- Attachment success, expiry/403, redirect rejection, oversize, truncation, collision, and verified reuse.
- Unknown component/embed/poll field fails the visible-completeness gate while source JSON remains preserved.
- Mutation tests that remove any nested component/embed/poll/snapshot visible
  field must fail the independent source-census gate even when IDs are unchanged.
- Duplicate/idempotent merge and edited-message replacement.
- 429 retry cap, pagination stall, partial live failure, crash before cutover, crash before cursor update, and lock contention.
- Fault injection at every journal/fsync/pointer/state/queue phase: restart sees
  only the full old or full new generation and cursor never leads archive data.
- Inventory missing one entry, partial archived-thread enumeration, duplicate
  path/channel identity, and messages arriving during scan.
- Lookback/full refresh where `after=cursor` is empty but edit/reaction/pin state changed.
- Searchability of one live and one retained local-only/deleted message.
- SSRF cases: loopback/private/link-local/metadata targets, userinfo, IP literal,
  IDN confusion, environment proxy, cross-host/private redirect, redirect loop,
  and proof that attachment requests contain no Bot authorization or cookies.
- Full-history dry-run and apply receipts; evidence checksum verification.

## Security scope and OWASP 2025 plan

SECURITY_SCOPE:

- data_classification: private Discord conversation content and attachments; secrets are never intentionally logged or committed.
- trust_boundaries: Discord API -> normalizer -> local staging -> verified archive; remote attachment CDN -> bounded downloader -> local bytes.
- roles_and_tenants: single trusted local operator; entry/channel boundaries must remain exact and path-contained.
- external_services_and_costs: Discord API and attachment CDN; bounded requests, retries, size, and runtime.
- ai_tools_and_write_capabilities: none in the archive implementation; deterministic code only.

OWASP_2025_PLAN:

- A01 PASS target: complete Discord inventory; exact message channel-to-entry
  binding; unique paths/channel IDs; no cross-entry writes; path containment and symlink rejection tests.
- A02 PASS target: restrictive file modes, private receipts, bounded configuration, no debug payloads in chat.
- A03 PASS target: stdlib-only implementation where practical; dependency and secret scans.
- A04 PASS target: SHA-256 corruption detection (not claimed as independent
  authenticity), HTTPS/CDN enforcement, no credentials in URLs/logs/Git.
- A05 PASS target: data is never interpreted as shell/HTML/path; raw HTML,
  Markdown marker, filename, Unicode, and control-character tests.
- A06 PASS target: immutable evidence, generation-pointer transaction, independent
  census and multi-dimensional completeness gates, no silent degradation.
- A07 PASS target: existing bot token remains confined to Discord API requests and
  is absent from every asset request, redirect, log, receipt, archive, and Git diff.
- A08 PASS target: checksummed evidence, canonical record hashes, attachment hashes, recomputation tests.
- A09 PASS target: bounded redacted receipts and actionable error counts without message bodies.
- A10 PASS target: 429, timeout, 403/404, partial success, crash, retry, lock, and rollback tests.
- business_logic_abuse_cases: path traversal filename; message-ID collision; stale edit overwriting new edit; cross-entry channel mismatch; partial fetch falsely marked complete; attachment URL SSRF/redirect; duplicate replay.
- AI_SECURITY_OVERLAY: not_applicable_with_reason — deterministic local archiver does not invoke a model or tools based on message content.
- ASVS_LEVEL_TARGET: not_applicable_with_reason — local CLI/Skill, not a Web/API application.
- ASVS_5_0_0_REQUIREMENT_REGISTER: not applicable; equivalent controls are the OWASP matrix and CLI threat cases above.
- HUMAN_SECURITY_GATES: live full-history cutover uses the user's already granted direct-repair authorization; deletion of retained evidence or legacy data requires separate approval.

## Delivery sequence

- Commit this design/task specification separately.
- Implement normalizer/renderer/downloader and incremental atomic merge on this branch.
- Add full-history rich rebuild and dual-gate verifier.
- Run targeted, full suite, package parity, secret/dependency scans, and independent review.
- Wait for any already-running ID reconciliation to release the shared lock.
- Deploy transactionally, run full-history rebuild, verify live closeout, then deploy managed cron/Qwen integration.
- Commit closeout evidence and merge/push only after all required gates pass.
