# Rich Core Adapter V3 — OWASP Top 10:2025 Gate

## Security scope

- Data: private Discord message payloads, local archive generations, state, and
  queue metadata. Tokens and message bodies must not enter public receipts.
- Trust boundaries: installed runtime files -> managed loader -> adapter -> rich
  core -> local archive; Discord transport -> normalizer.
- Roles: one local operator process; every entry remains bound to one stable
  guild/channel/path inventory identity.
- AI overlay: not applicable; the adapter is deterministic and invokes no model.
- ASVS: not applicable; this is a local CLI rather than a Web/API service.

## A01–A10 evidence targets

- A01: canonical lock, exact root/entry/inventory binding, opaque single-use
  authority, no cross-entry or alternate-lock mutation.
- A02: owner-controlled single-link runtime files, private lock/state/receipt
  modes, no secret-bearing diagnostics.
- A03: stdlib-only adapter/loader and dependency audit of the shipped package.
- A04: SHA-256 runtime component and CURRENT/generation bindings; hashes are
  integrity evidence, not remote authenticity claims.
- A05: exact request types, path containment, control/symlink/hardlink rejection,
  payloads treated only as data.
- A06: fail-closed contract/version checks, immutable capability registry,
  queue-before-state crash invariant.
- A07: Discord Bot authorization stays in Discord API requests and is never sent
  to attachment endpoints, logs, manifests, or Git.
- A08: exact source hashes, CURRENT pointer hash, generation hash, and readback
  recomputation before state authorization.
- A09: fixed redacted error enums and bounded count-only public output.
- A10: bounded requests, 429 budget, capability TTL, lock contention, partial
  pagination, and failure-injection tests.

## Abuse cases

- Substitute an alternate lock path or archive root.
- Forge matching commit and CURRENT Mapping receipts.
- Replay a grant after use/close/fork/expiry.
- Advance cursor beyond committed new IDs or during mutable-only refresh.
- Replace a verified runtime component with a symlink, hardlink, mode-drifted, or
  same-name different-hash file.
- Crash after CURRENT publication but before queue/state persistence.

Any unresolved P0/P1, runtime component mismatch, authority bypass, cursor-ahead
state, or unredacted secret blocks integration and deployment.
