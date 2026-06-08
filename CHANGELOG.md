# Changelog

## Unreleased

- Fix backlog worker stale-healthy coverage so quiet channels that become active again are probed with `healthy_stale_probe`.
- Keep queue cursors monotonic with state cursors to prevent duplicate raw appends from stale queue items.
- Limit stale probe queue upserts to the entries selected for the current bounded run.
- Add backlog worker selection regression tests for stale probes and cursor monotonicity.

## 1.0.0 - planned

- Initial OpenClaw Discord backup skill.
- V3 state with `lastWrittenMessageId` cursor.
- Backlog queue.
- Deterministic backlog worker.
- Full caught-up audit probe.
- Customer install/config skeleton.
- Optional LanceDB post-backup incremental indexing integration.
