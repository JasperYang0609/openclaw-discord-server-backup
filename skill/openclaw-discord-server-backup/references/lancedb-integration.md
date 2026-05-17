# LanceDB Integration

## Purpose

This backup skill can maintain an existing OpenClaw LanceDB knowledge layer after Discord backups are written.

Recommended customer install order:

1. Install `openclaw-lancedb-knowledge`.
2. Configure its source map to include the Discord backup root, especially `summary/` folders.
3. Run a baseline LanceDB scan/index.
4. Install `openclaw-discord-server-backup`.
5. Enable `lancedb.runAfterBackup` in this backup skill config.

## Boundary

This skill does not replace the LanceDB skill. It only calls the configured LanceDB incremental command after backup jobs.

## Config fields

- `lancedb.enabled`
- `lancedb.projectPath`
- `lancedb.incrementalCommand`
- `lancedb.latestManifest`
- `lancedb.runAfterBackup`
- `lancedb.externalEmbeddingsRequireApproval`

## Cron placement

Recommended order:

- Discovery
- Daily backup
- Backlog worker
- Audit
- LanceDB incremental indexing

For most customers, run LanceDB incremental after the backup/audit window, for example 06:30 or later.

## Privacy

If the LanceDB project uses external embeddings, get explicit customer approval before indexing private Discord backup content.

Prefer indexing summaries first. Index raw chat only when summaries are insufficient.

## Script

Run `scripts/run_lancedb_incremental.py` with the customer config and workspace path. The script returns JSON with command result and the latest LanceDB manifest when available.
