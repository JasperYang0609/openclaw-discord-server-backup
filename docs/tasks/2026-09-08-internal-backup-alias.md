# Internal Backup Alias Compatibility Task

## Goal

Allow the reviewed Daily Backup installer to upgrade an existing archive that has a
single safe, internal directory alias without weakening the existing no-follow and
rollback guarantees.

## In scope

- Raw-tree integrity hashing for internal directory aliases.
- Positive and hostile-path regression tests.
- Full release and live cutover verification.

## Out of scope

- Moving or deleting archive data.
- Changing state `relativePath` values.
- Allowing external, broken, chained, file, or unowned links.
- Changing schedules, message limits, report routing, or Qwen behavior.

## Acceptance

- Existing live alias passes the pre-mutation tree hash.
- Unsafe aliases fail before cron mutation.
- Full repository self-check passes.
- Independent review reports no open P0-P3.
- Live retry returns READY, exact 11/11 readback passes, and legacy jobs remain
  disabled rather than deleted.
