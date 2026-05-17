# Architecture

## Goal

Back up Discord channels and threads in OpenClaw without silently missing messages.

## Components

- Discovery: register channels and threads, create folders, detect naming anomalies.
- Daily sync: bounded reads for healthy entries.
- Backlog worker: queue-first catch-up for heavy or partial entries.
- Audit: full live probe across all registered entries.
- LanceDB indexing: optional incremental knowledge index after backup files are written.

## Completion rule

An entry is complete only when a read after the stored cursor returns 0 messages.

## Write order

1. append raw
2. append summary
3. update state cursor
4. update queue

Never update state ahead of raw.
