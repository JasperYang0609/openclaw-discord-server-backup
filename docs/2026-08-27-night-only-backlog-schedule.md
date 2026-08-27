# Night-only backlog schedule

## Decision

Routine Discord backlog catch-up runs at 23:10 and hourly from 00:10 through
04:10 in the customer timezone:

```cron
10 0,1,2,3,4,23 * * *
```

The previous 06:10, 11:10, and 17:10 runs are removed. Worker limits remain
`max-entries=4`, `max-batches=12`, `max-batches-per-entry=5`, and `limit=100`.
Unfinished queue debt remains durable and resumes the following night.

## Alternatives considered

- One oversized midnight run was rejected because a long exclusive run creates a
  larger failure and resource-contention window.
- Keeping a low-frequency daytime run was rejected because the reported problem is
  interactive task slowdown while backlog is active.

## Acceptance

- Public and packaged guidance use the same cron expression.
- No routine backlog run occurs from 05:00 through 22:59.
- The 05:15–06:30 core/discovery/daily/audit/LanceDB pipeline has no scheduled
  backlog overlap.
- Cursor, queue, write-before-state, retry, and worker-limit behavior is unchanged.
- A manual incident run is allowed only with the same bounded limits.

## Security and reliability scope

This is a scheduling and documentation change for an automation skill; it adds no
network endpoint, identity system, tenant boundary, secret, dependency, or live-data
mutation. ASVS is not applicable because the repo is not a Web/API application.

- A01, A04, A05, A07: not applicable; no access-control, cryptographic, input, or
  authentication surface changes.
- A02, A06, A10: pass through an exact schedule contract, preserved bounded limits,
  durable resume, and explicit non-overlap with the daily pipeline.
- A03: pass; no dependency or executable supply-chain change.
- A08: pass through package parity and regression tests.
- A09: pass; existing worker reports and `auditWarnings` remain unchanged.
- AI overlay: the worker still uses the same deterministic script and least-privilege
  tool surface; no model-generated state transition is introduced.

Business-logic negatives cover recurrence during daytime, reintroduction of the old
schedule, removal of bounded limits, and forcing unfinished debt into one long run.
Changing already-installed customer cron jobs remains a separate operator action and
is not performed by this repository commit.
