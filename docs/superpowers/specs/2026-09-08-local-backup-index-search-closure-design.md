# Local Backup → Incremental Index → Search Closure

Status: approved by Jasper on 2026-09-08; implementation authorized.

## Goal

Prove on the current Apple Silicon Mac that new Discord messages are durably
backed up, incrementally indexed by local Qwen/LanceDB, and retrievable with a
source citation. Client packaging is a separate follow-up project and cannot
start until this local closure passes.

## Evidence at design time

- Qwen runtime, embedding canary, OpenClaw integration, index state, incremental
  cron, and verified-snapshot cron are healthy.
- The index source map includes Discord raw Markdown.
- Of 182 Discord entries, 168 are healthy, 11 have new messages after their
  durable cursor, and three previously failed reads now succeed.
- Three legacy raw archives contain duplicate message IDs. This is duplicate
  evidence, not proof of message loss, but it must not create duplicate search
  results.
- The current rich-archive daily runner blocks because a complete rich baseline
  is absent. Building 182 rich baselines is not required to restore the core
  daily backup and search contract.

## Selected approach

Separate the core pipeline from optional rich archival.

1. Use the existing deterministic V3 backlog worker to catch up only entries
   with verified messages after their cursor.
2. Preserve append-only raw archives and move cursors only to messages that were
   written and read back successfully.
3. Treat rich attachment/embed archival as an independent feature. Missing rich
   baselines must report a warning or pending state but must not block core raw
   message backup.
4. Run the existing local Qwen/LanceDB incremental command after the backup
   audit window.
5. Search for a unique phrase from a newly backed-up message and require the
   correct Discord raw source path in the result.
6. Verify that legacy duplicate IDs do not create duplicate indexed chunks or
   duplicate top results. Repair only the three affected archives if the index
   deduplication contract is insufficient; never rewrite unrelated history.

## Daily data flow

`Discord discovery → deterministic core/raw incremental backup → caught-up audit → local Qwen incremental index → verified snapshot → one health report`

Rich content follows a separate path:

`optional rich baseline/incremental archive → its own receipt and warning state`

The rich path cannot advance the core cursor and cannot turn a failed core
backup green.

## Safety and scope

- No cloud embedding provider.
- No full 182-entry rich rebuild in this phase.
- No deletion of Discord history or unknown cron jobs.
- No cursor advance before durable raw write and readback.
- No state/queue repair based only on dates or assumptions.
- One worker at a time on this 16 GiB host.
- Qwen model identity, dimensions, pooling, normalization, and existing index
  identity remain unchanged.
- Work occurs on a dedicated Git branch. Implementation, tests, evidence, and
  closeout require commits and a clean pushed worktree.

## Acceptance gates

The local phase is complete only when all of the following pass:

- The 11 currently detected message debts are durably caught up; a fresh
  `read after=<cursor>` returns zero for every eligible entry.
- The three prior read errors remain readable or are reported as explicit
  current errors without false cursor advancement.
- Local raw integrity has no unexplained missing IDs; known duplicate IDs are
  either deduplicated at index ingestion or repaired with preserved evidence.
- A fresh Qwen incremental run succeeds and produces a current trusted receipt.
- A unique newly backed-up phrase is found within top five results with the
  correct Discord source path; repeated searches are deterministic enough for
  the release threshold.
- Repository tests, package/source parity, secret scan, dependency audit,
  OWASP A01–A10 register, installed post-run check, exact cron readback, and an
  isolated restore check pass.
- The next natural 05:25–07:05 pipeline completes without core backup or index
  errors. Optional rich state may be reported separately and may not falsify the
  core result.

## Client packaging follow-up

After local acceptance, create a separate Apple Silicon Mac project with:

- a deterministic installer for M1/M2/M3/M4/M5-class Macs;
- a data-free application/skill package;
- an encrypted customer data transfer bundle containing source data,
  configuration, manifests, and restore evidence;
- import, verification, and rebuild-from-source paths;
- a clean-machine installation and restore test.

Windows, Linux, Intel Mac, and cloud embeddings are out of scope for the first
customer release.
