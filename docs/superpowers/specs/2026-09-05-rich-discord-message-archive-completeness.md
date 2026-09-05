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
- Two independent completion gates: message-ID coverage and visible-payload fingerprint coverage.
- Fail-closed handling for pagination stalls, truncated responses, unknown visible structures, attachment errors, and crash-before-cutover.

Out of scope:

- Deleting legacy or duplicate history.
- Guessing content for Discord messages already deleted before the repair and absent from every local copy.
- Mutating Discord messages or server configuration.
- Changing the summary generation model or publishing private archive data externally.

## Storage contract

Each entry keeps its existing directory. New canonical content is written under:

- `canonical/YYYY-MM-DD.jsonl`: one sorted JSON object per message.
- `raw/YYYY-MM-DD.md`: deterministic readable rendering from the canonical record.
- `attachments/<message-id>/<safe-filename>`: downloaded bytes.
- `receipts/rich-archive-latest.json`: entry-level verification and attachment status.

Before the first rich rebuild, the prior `raw/` and any prior `canonical/` trees are copied into a checksummed, read-only evidence snapshot outside the live archive tree. Rebuild output is created in a sibling staging directory, verified, fsynced where supported, then exchanged into place per entry. The previous live directories are moved to a uniquely named quarantine/evidence location only after the verified replacement exists. No unverified output replaces the live archive.

Canonical JSONL records contain:

- schema version, message ID, channel ID, timestamps, type, flags, pin/TTS state;
- author identity fields returned by Discord;
- text content and mentions;
- recursively normalized components;
- embeds, poll, sticker items, forwarded/message snapshots, reply/reference context;
- interaction/system metadata and reactions;
- attachment metadata, original URL, local relative path, byte length, and SHA-256;
- `sourcePayloadSha256` over the normalized source subset;
- `visiblePayloadSha256` over the normalized human-visible subset;
- explicit `unknownVisibleFields` and `attachmentErrors` arrays.

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

`(無文字內容)` may be used only when the normalized visible payload is truly empty. A component-only or embed-only message must never render as empty.

## Attachment policy

- Download through HTTPS only, with bounded redirects, timeouts, per-file byte limit, and no ambient credentials.
- Use server filename only after path sanitization; namespace by message ID to avoid collisions.
- Stream to a temporary regular file, hash while writing, verify declared size when available, then atomically rename.
- Reuse an existing local file only when its stored SHA-256 and byte length verify.
- A missing, expired, oversized, truncated, or hash-mismatched attachment is recorded and makes the relevant entry and full run non-PASS. It must not be silently ignored.
- Remote URLs remain metadata, but a URL alone does not satisfy attachment completeness.

## Rebuild transaction

1. Acquire the existing shared backup lock before any mutation.
2. Freeze state/queue and the complete pre-repair raw/canonical trees into immutable SHA-256 evidence.
3. Fetch every page for every non-excluded channel. Reject duplicate/stalled pagination and any live error.
4. Normalize each message, download/verify attachments, and write staged JSONL and Markdown.
5. Validate per entry:
   - live IDs equal canonical IDs;
   - exactly one canonical record per live ID;
   - every canonical source and visible fingerprint recomputes;
   - Markdown contains exactly one block for each canonical ID;
   - every attachment referenced as complete exists and hashes correctly;
   - no unknown visible field is unaccounted for.
6. Atomically cut over only the entries that fully validate. A full-run receipt remains FAIL unless every selected entry passes.
7. Enrichment-only rebuilds do not advance cursors. Cursor updates remain reserved for successfully committed new-ID writes.
8. Verify the immutable evidence after cutover and retain it for rollback/review.

## Future incremental writes

`append_batch` is replaced by an idempotent merge:

- acquire the shared lock;
- load the day's canonical records;
- normalize incoming messages and merge by ID;
- newer `edited_timestamp` wins for the same ID; equal versions must have equal source hashes;
- write staged JSONL and regenerate Markdown atomically;
- only after both files and attachments verify may state/queue cursors advance.

This prevents duplicate append noise and captures edits when a message is seen again.

## Verification and completion gate

The repair is complete only when a post-cutover live closeout reports all of the following:

- `liveErrors = 0`
- `idCoverage = 100%`
- `visibleFingerprintCoverage = 100%`
- `markdownCoverage = 100%`
- `duplicateCanonicalIds = 0`
- `unknownVisibleFields = 0`
- `attachmentErrors = 0`
- immutable pre-repair evidence verification PASS
- crash/restart, 403/404, 429, pagination-stall, unknown-type, duplicate, and edit tests PASS

Legacy duplicate IDs are retained in evidence and reported separately. They are not deleted during this change.

## Required tests

- Plain text and empty system message.
- Component-only type 17/10 message with nested text, button labels, select labels, placeholder, and URL.
- Embed-only message including title, description, fields, author/footer, and media metadata.
- Poll, sticker, forwarded/message snapshot, reply reference, interaction/system metadata, reactions, pin, and edited timestamp.
- Unicode, Markdown metacharacters, multiline content, and hostile path-like filenames.
- Attachment success, expiry/403, redirect rejection, oversize, truncation, collision, and verified reuse.
- Unknown component/embed/poll field fails the visible-completeness gate while source JSON remains preserved.
- Duplicate/idempotent merge and edited-message replacement.
- 429 retry cap, pagination stall, partial live failure, crash before cutover, crash before cursor update, and lock contention.
- Full-history dry-run and apply receipts; evidence checksum verification.

## Security scope and OWASP 2025 plan

SECURITY_SCOPE:

- data_classification: private Discord conversation content and attachments; secrets are never intentionally logged or committed.
- trust_boundaries: Discord API -> normalizer -> local staging -> verified archive; remote attachment CDN -> bounded downloader -> local bytes.
- roles_and_tenants: single trusted local operator; entry/channel boundaries must remain exact and path-contained.
- external_services_and_costs: Discord API and attachment CDN; bounded requests, retries, size, and runtime.
- ai_tools_and_write_capabilities: none in the archive implementation; deterministic code only.

OWASP_2025_PLAN:

- A01 PASS target: exact state inventory only; no cross-entry writes; path containment and symlink rejection tests.
- A02 PASS target: restrictive file modes, private receipts, bounded configuration, no debug payloads in chat.
- A03 PASS target: stdlib-only implementation where practical; dependency and secret scans.
- A04 PASS target: SHA-256 integrity, HTTPS-only downloads, no credentials in URLs/logs/Git.
- A05 PASS target: data is never interpreted as shell/HTML/path; filename and Markdown-safe rendering tests.
- A06 PASS target: immutable evidence, atomic cutover, dual completeness gates, no silent degradation.
- A07 NOT_APPLICABLE_WITH_EVIDENCE: uses existing local Discord bot authentication; this change adds no authentication surface.
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
