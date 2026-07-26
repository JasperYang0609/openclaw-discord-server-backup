# Core Backup Prompt

Goal: back up the workspace core Markdown files and durable memory to the designated backup root.

## Required placeholders

- `{{WORKSPACE_ROOT}}`: the customer OpenClaw workspace
- `{{BACKUP_ROOT}}`: the customer backup root

Resolve both paths before copying. Do not continue while either placeholder is unresolved.

## What to back up

- Every `.md` file directly under `{{WORKSPACE_ROOT}}` (root level only; do not recurse)
- The entire `{{WORKSPACE_ROOT}}/memory/` folder

Do not hardcode filenames. Discover the root-level Markdown files at runtime so customer-specific core files are included automatically.

## Destinations

- Latest: `{{BACKUP_ROOT}}/核心文件/latest/` — refresh on every successful run
- Daily snapshot: `{{BACKUP_ROOT}}/核心文件/snapshots/YYYY-MM-DD/` — create once per local calendar day

Create destination folders when needed. If today's snapshot already exists, leave it unchanged and report `已存在（跳過）`.

## Safe write order

1. Verify `{{WORKSPACE_ROOT}}` exists and `{{WORKSPACE_ROOT}}/memory/` is a readable directory.
2. Discover the root-level `.md` source files and record the count as `N`.
3. Copy the discovered `.md` files and `memory/` into `latest/`.
4. If today's snapshot did not exist at run start, copy the same source set into that snapshot.
5. Verify `latest/` contains all `N` root-level `.md` files and a readable `latest/memory/` directory.
6. For a newly created snapshot, verify the same source set there.
7. Retry each missing item once. If anything is still missing, report `❌ 失敗` and list only the missing relative paths.

Never delete unrelated files outside the two destination directories. Never upload, post, or commit backup contents; core files may contain private configuration.

## Report format

```text
狀態：✅ 完整 / ⚠️ 補做後通過 / ❌ 失敗
備份：根目錄 md N 個、memory/ 資料夾 1 個
快照：已建立 / 已存在（跳過）/ ❌ 失敗
缺漏：無 / <relative paths>
```
