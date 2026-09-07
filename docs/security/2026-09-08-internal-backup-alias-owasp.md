# Internal Backup Alias Security Closeout

## Scope and threat model

- Asset: existing Discord raw/summary archive and its transactional integrity hash.
- Trust boundary: filesystem entries beneath the configured raw backup root.
- Abuse cases: alias escape, dangling link, link chain, link-to-file, ownership drift,
  alias replacement during upgrade, and silent archive mutation.
- Web/API surface: none; ASVS v5.0.0 is not applicable with evidence because this is
  a local CLI installer and filesystem transaction.
- AI overlay: not applicable; no model output controls paths or mutations.

## OWASP Top 10:2025

- A01 PASS: alias destinations and archive enumeration are descriptor-bound beneath
  the opened raw root; parent, root, and target replacement regressions fail closed.
- A02 PASS: only one explicit internal-directory alias shape is permitted.
- A03 PASS: the release package was rebuilt from the final source and exact package
  parity passed in the repository self-check.
- A04 NOT_APPLICABLE_WITH_EVIDENCE: no credential or cryptographic protocol changes.
- A05 PASS: path values are structured filesystem objects; no shell interpolation is added.
- A06 PASS: every traversed directory and file uses `dir_fd` plus `O_NOFOLLOW`, with
  identity revalidation and a narrow internal-directory-alias allowlist.
- A07 NOT_APPLICABLE_WITH_EVIDENCE: no authentication surface exists.
- A08 PASS: link/target identities join the archive hash and each alias target
  identity must equal the canonical directory identity used for content traversal.
- A09 PASS: rejected alias classes produce explicit non-secret installer errors.
- A10 PASS: missing, changed, retargeted, parent-swapped, root-replaced, and
  target-replaced entries stop before mutation or force rollback; agent-message
  normalization is applied before exact staged-contract verification.

## Closeout gate

- Business-logic negatives: external, broken, chained, file, root-level, ownership
  mismatch, retargeted alias, parent replacement, raw-root replacement, and alias
  target replacement.
- Verification: 243/243 tests PASS; post-run check PASS including package/source
  parity, cron-manifest validation, and both restore smokes. Evidence:
  `logs/tool-runs/20260908_015900_daily-message-final-full-pytest.log` and
  `logs/tool-runs/20260908_020005_daily-message-final-postcheck.log`.
- Static checks: Python compilation, diff whitespace, and changed-content secret-shape
  scan PASS. Evidence:
  `logs/tool-runs/20260908_020324_daily-message-final-static.log`.
- Live read-only archive-tree verification: PASS. Evidence:
  `logs/tool-runs/20260908_013300_daily-alias-final-live-tree.log`.
- Live disabled temporary agent-message readback matched the exact normalized
  contract and was removed. Evidence:
  `logs/tool-runs/20260908_015945_daily-message-live-readback.log`.
- Independent release-quality review: three race regressions, live alias identity,
  complete tests, package parity, and self-check PASS.
- Open P0/P1/P2/P3: 0/0/0/0.
- Release decision: RELEASE_READY for commit and the already-authorized controlled
  cutover; live readback and natural-schedule validation remain separate gates.
