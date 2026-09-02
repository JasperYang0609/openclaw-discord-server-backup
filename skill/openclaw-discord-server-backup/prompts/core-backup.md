# Core Workspace Backup Prompt

Goal: run the repository-owned deterministic backup engine for workspace core Markdown files and durable memory.

## Required placeholders

- `{{WORKSPACE_ROOT}}`: the customer OpenClaw workspace
- `{{BACKUP_ROOT}}`: a backup root that does not overlap the workspace

For the default customer layout, `{{BACKUP_ROOT}}` is the real Desktop directory
`~/Desktop/<Discord伺服器名稱>資料備份` reported by the installer. Do not use its
`Discord資料/` child here; the core engine owns the sibling `核心文件/` tree.

Resolve and validate both paths before running. Do not continue while either placeholder is unresolved. Do not manually recreate the copy/state logic in this prompt.

## Run

```bash
python3 "{{WORKSPACE_ROOT}}/skills/openclaw-discord-server-backup/scripts/core_workspace_backup.py" backup \
  --workspace "{{WORKSPACE_ROOT}}" \
  --backup-root "{{BACKUP_ROOT}}"
```

The engine is the source of truth. It:

- discovers every root-level `.md` file without hardcoded names;
- includes the complete `memory/` tree, including empty directories;
- rejects source symlinks, special files, unsafe dates, and overlapping source/destination paths;
- stages and verifies `核心文件/latest/` before replacing the previous latest tree;
- creates at most one immutable `核心文件/snapshots/YYYY-MM-DD/` tree per local calendar day;
- emits `.backup-manifest.json` with exact paths, byte counts, and SHA-256 hashes;
- fails closed when an existing daily snapshot is missing, extra, corrupted, or tampered.

A successful `backup` command already verifies both `latest/` and the selected daily snapshot. Do not report success from file counts or timestamps alone.

## Restore canary

After backup, verify a temporary isolated restore of latest:

```bash
python3 "{{WORKSPACE_ROOT}}/skills/openclaw-discord-server-backup/scripts/core_workspace_backup.py" restore-canary \
  --backup-dir "{{BACKUP_ROOT}}/核心文件/latest"
```

The canary uses an automatically removed temporary directory and never writes into the real workspace. If it fails, report the backup as unsafe for recovery.

## Safety

- Never delete or overwrite daily snapshots.
- Never restore directly into the customer workspace from this job.
- Never upload, post, or commit backup contents; core files may contain private configuration.
- Never bypass a manifest, symlink, overlap, missing-file, extra-file, or hash failure.

## Report format

```text
狀態：✅ 完整 / ❌ 失敗
備份：根目錄 md N 個、memory 檔案 N 個
快照：已建立 / 已存在且驗證
Manifest：✅ exact paths + SHA-256 / ❌ 失敗
還原 canary：✅ 通過 / ❌ 失敗
錯誤：無 / <redacted error summary>
```
