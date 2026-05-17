# LLM Handoff

This skill is for OpenClaw Discord backups. It can optionally trigger LanceDB incremental indexing after backups.

Any LLM operating this skill should follow these rules:

- Use deterministic scripts for state, queue, backlog, and audit work.
- Do not invent state transitions manually.
- Do not treat `lastBackup` as proof of completion.
- A backup entry is caught up only when read-after-cursor returns 0.
- Use fixed status and reason enums from `state-schema.md`.
- If configuration is missing, ask for the missing config instead of guessing.

Most customer agents may use Claude Opus, but the skill must remain model-neutral. Use explicit commands and plain language.
