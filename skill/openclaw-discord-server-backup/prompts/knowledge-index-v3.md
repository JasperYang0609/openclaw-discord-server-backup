# Knowledge Index V3 Prompt

Goal: update the optional LanceDB knowledge layer after Discord backup files are written.

Prerequisite: LanceDB/OpenClaw knowledge project is already installed. This backup skill does not replace the LanceDB skill; it calls the configured incremental index command.

Action:
1. Read the customer backup config.
2. If `lancedb.enabled` is false, report skipped.
3. Run `scripts/run_lancedb_incremental.py --config <config> --workspace <workspace>`.
4. Summarize the JSON result.

Report:
- ok / skipped / failed
- changed files
- added chunks
- rows after indexing
- secret hit files

Do not run a full reindex unless the LanceDB project itself decides no baseline exists.
