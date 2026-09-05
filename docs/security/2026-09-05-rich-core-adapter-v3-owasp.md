# Rich Core Adapter V3 — OWASP Top 10:2025 Gate

## Security scope

- Data: private Discord message payloads, local archive generations, state, and
  queue metadata. Tokens and message bodies must not enter public receipts.
- Trust boundaries: installed runtime files -> managed loader -> adapter -> rich
  core -> local archive; canonical user config -> under-lock config binder;
  Discord transport -> normalizer.
- Roles: one local operator process; every entry remains bound to one stable
  guild/channel/path inventory identity.
- AI overlay: not applicable; the adapter is deterministic and invokes no model.
- ASVS: not applicable; this is a local CLI rather than a Web/API service.

## A01–A10 evidence targets

- A01: canonical lock, exact root/entry/inventory binding, opaque single-use
  authority, no cross-entry or alternate-lock mutation.
- A02: owner-controlled single-link runtime/config files, private
  lock/state/receipt modes, no secret-bearing diagnostics.
- A03: stdlib-only adapter/loader and dependency audit of the shipped package.
- A04: SHA-256 runtime component and CURRENT/generation bindings; hashes are
  integrity evidence, not remote authenticity claims.
- A05: exact request types, path containment, control/symlink/hardlink rejection,
  payloads treated only as data.
- A06: fail-closed contract/version checks, immutable capability registry,
  queue-before-state crash invariant.
- A07: Discord Bot authorization stays in Discord API requests and is never sent
  to attachment endpoints, logs, manifests, or Git.
- A08: exact source hashes, CURRENT pointer hash, generation hash, static
  runtime hashes, and independently derived dynamic-config hash; all read back
  before state authorization.
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
- Supply a fake config hash or replace the canonical config after the lock is
  acquired.
- Crash after CURRENT publication but before queue/state persistence.

Any unresolved P0/P1, runtime component mismatch, authority bypass, cursor-ahead
state, or unredacted secret blocks integration and deployment.

## Verification register

- A01 PASS: canonical-lock, cross-entry, cross-session, PID/fork, replay, and
  CURRENT-drift regressions reject substituted authority.
- A02 PASS: runtime manifest, core, adapter, runner, and dispatcher require
  effective-user ownership, one link, regular-file identity, and mode `0600`;
  managed config uses the same private-file controls.
- A03 PASS: the production adapter and loader use only Python standard-library
  modules; the complete repository test suite and packaged-layout smoke pass.
- A04 PASS: runtime component SHA-256, exact CURRENT bytes, generation digest,
  normalized source digest, inventory digest, and independently derived config
  digest are bound and re-read at the relevant authority transition.
- A05 PASS: exact dataclass/schema checks, Unicode control rejection, safe-root
  containment, and symlink/hardlink/path substitution regressions pass.
- A06 PASS: unsupported contracts and full-rebuild APIs fail closed; persisted
  Mapping receipts remain `AUDIT_ONLY`; queue-before-state fault tests pass.
- A07 PASS: token loading is confined to the Discord API transport; no token is
  accepted by the rich core or attachment downloader, manifest, receipt, or
  state-update grant.
- A08 PASS: source/package/runtime-manifest byte parity and installed-layout
  verification pass for all four production runtime components.
- A09 PASS: public failures are fixed categories; CLI output contains status
  and bounded counts only, without payloads, paths, or credentials.
- A10 PASS: entry/page/message/response/rate-limit/capability budgets are
  bounded; around-window accounting includes rows filtered from the merge set;
  lock contention and partial pagination fail safely.

ASVS v5.0.0: `NOT_APPLICABLE_WITH_EVIDENCE` because this component is a local
single-user CLI and exposes no web application or network service endpoint.
