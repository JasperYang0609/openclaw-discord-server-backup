# Internal Backup Alias Compatibility Design

## Context

The live backup archive contains one historical directory alias created when a
Discord customer path was renamed. The alias points to the canonical directory
inside the same backup root. The transactional installer currently rejects every
symlink before cron mutation, so a safe existing upgrade cannot begin.

## Decision

Keep the fail-closed boundary while supporting this narrow legacy shape. The raw
tree integrity hash may accept a symlink only when all of these conditions hold:

- the link and destination are owned by the current account;
- the destination is lexically inside the same raw backup root;
- the destination exists and is a real directory;
- no destination path component is another symlink; and
- each destination component is opened relative to the already-open raw-root
  descriptor with `O_NOFOLLOW`, so a concurrent parent swap cannot escape.

The archive enumeration itself also starts from that already-open root descriptor;
it does not reopen the raw-root pathname while walking. Directory contents are
relisted after hashing and the root descriptor identity is compared with the live
root pathname, so replacement during one integrity read fails closed.
Alias target identities are also joined to the identity recorded when the same
canonical directory is traversed. This prevents a stable alias from contributing
one inode identity while a replacement directory contributes different contents.

The hash records the link path, literal target, canonical in-root target, and both
link and target device/inode identities. Directory contents remain hashed once at
their canonical path; the alias is never traversed. External, broken, chained, file,
root-level, or ownership-mismatched links remain blocked.

## Transaction behavior

The installer computes this hash before and after its controlled cron and Skill
mutation. A changed link, destination identity, or archive file still forces
rollback. Existing backup data and state paths are not migrated or rewritten.

## Verification

- Positive regression for an owner-controlled in-root directory alias.
- Negative regressions for external, broken, chained, and file aliases.
- Retargeting regression proving a link redirected to a different internal directory
  changes the integrity hash.
- Deterministic regression for a destination parent swapped to an external symlink,
  an explicit link-to-root rejection, and replacement of the raw-root pathname
  immediately after its descriptor is opened.
- Deterministic regression for replacing a real internal alias target after its
  descriptor is opened but before the canonical directory walk.
- Full installer, cron topology, packaging, restore, and security gates before live
  retry.

The user previously authorized autonomous completion and requested a final report
only; this compatibility repair stays within that approved backup-cutover scope.
