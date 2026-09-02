# Customer Desktop Backup Root Design

Date: 2026-09-02  
Status: approved design, implementation pending

## Problem

The customer installer currently writes a default `backupRoot` value but does not
create that directory. It also has no rule for creating a customer-visible backup
folder on the macOS Desktop. As a result, a customer can finish installation while
seeing no backup folder on the Desktop, even though the configuration points to
`~/OpenClawBackups/discord`.

The repository already contains the deterministic core-workspace backup engine for
root-level Markdown files and the complete `memory/` tree. However, installation does
not automatically connect that engine to a customer-visible folder structure.

## Approved outcome

For a fresh customer installation on macOS, the installer creates one real backup
root on the Desktop. Its name is the Discord server display name followed immediately
by `資料備份`.

Example:

```text
~/Desktop/南方資料備份/
├── Discord資料/
└── 核心文件/
    ├── latest/
    └── snapshots/
```

This is a real directory, not a symlink or Finder alias.

## Naming contract

- The installer accepts the Discord server display name as an explicit required
  input for a fresh default installation.
- The generated directory name is `<Discord伺服器名稱>資料備份`.
- Leading and trailing whitespace is removed from the supplied server name.
- Empty names, `.` and `..`, path separators, NUL bytes, control characters, and
  names that resolve outside the selected Desktop directory are rejected.
- The display name is never used as the stable Discord identity. Runtime channel and
  thread mapping continues to use Discord IDs.
- A caller may still supply an explicit custom backup root. When it does, the
  explicit path wins and the installer does not silently create a second Desktop
  backup root.

## Directory and configuration contract

- The customer backup root is `~/Desktop/<Discord伺服器名稱>資料備份` by default.
- Discord backup content is rooted at `<backup root>/Discord資料`.
- Core workspace backup content is rooted at `<backup root>` so the existing engine
  continues to own `<backup root>/核心文件/latest` and
  `<backup root>/核心文件/snapshots/YYYY-MM-DD`.
- The generated customer config records the absolute Discord data root.
- The generated state `rootPath` matches the configured Discord data root rather than
  retaining the old template value.
- The installer output reports the created backup root, Discord data root, core data
  root, config path, state path, and queue path without exposing secrets.

## Core memory backup contract

The existing core-workspace engine remains the only implementation of core memory
backup. The installer and cron materials call it; they must not reimplement its copy
logic.

Its scope remains:

- every root-level Markdown file in the OpenClaw workspace;
- the complete `memory/` directory tree, including empty directories;
- a manifest-verified replaceable `核心文件/latest/` tree;
- at most one immutable `核心文件/snapshots/YYYY-MM-DD/` tree per local day;
- SHA-256, byte-count, missing-file, extra-file, overlap, and symlink validation;
- an isolated restore canary that never restores into the live workspace.

Project repositories, dependencies, secrets, `.env` files, and arbitrary customer
data remain outside this narrow core scope. Recovery-critical workspace assets use
the existing explicit-scope recovery snapshot tools.

## Installer behavior

Fresh installation:

1. Validate the workspace, Desktop root, server name, and all derived paths.
2. Refuse unsafe path overlap with the OpenClaw workspace.
3. Create the real Desktop backup root and `Discord資料/` idempotently.
4. Create config, state, and queue scaffolding with matching paths.
5. Copy the skill.
6. Print the remaining cron and live-audit setup steps.

Existing installation:

- Do not move or rename an existing backup tree automatically.
- Do not overwrite non-empty configuration or state without the existing explicit
  force/migration workflow.
- If the configured root differs from the new Desktop default, report a migration
  requirement and leave customer data untouched.
- A future migration command must use copy, manifest verification, read-back, and
  explicit cutover before the old root is retired. Migration is outside this change.

## Error handling and safety

- Fail before writing when the server name or derived path is unsafe.
- Refuse a backup root that is a symlink, a file, or overlaps the workspace.
- Re-running a successful fresh install is idempotent for directories and must not
  erase backup content.
- Never delete or overwrite immutable daily snapshots.
- Never place tokens, Discord exports, customer config, or backup data in Git logs,
  tests, fixtures, or release artifacts.
- Installer failure must clearly identify the failed stage and leave existing backup
  data unchanged.

## Documentation changes

Update all customer-facing sources of truth together:

- repository `README.md`;
- installed skill `SKILL.md`;
- `references/customer-install.md`;
- installer help and output;
- config examples and cron guidance where the backup root appears.

The documentation must state that the Desktop folder is created automatically only
for the default fresh-install path. It must also explain that core memory backup is
already implemented but requires the core-backup cron job to run.

## Acceptance tests

Automated tests must cover:

- `南方` produces a real `~/Desktop/南方資料備份/` directory;
- `Discord資料/` is created and config/state paths agree;
- the core backup engine writes `核心文件/latest/` and one immutable daily snapshot
  under the same customer root;
- installation into a temporary fake home/Desktop does not touch the developer's
  real Desktop;
- rerunning does not remove existing backup files;
- unsafe names and symlink/file destinations fail closed;
- explicit custom backup roots remain supported and do not create an extra Desktop
  root;
- existing configured roots are not silently moved;
- package parity and the full post-run self-check pass.

Manual acceptance on a clean macOS test account must confirm:

- the named folder is visible on the Desktop;
- Discord backup content appears under `Discord資料/` after the scheduled or manual
  backup run;
- core Markdown and `memory/` content appears under `核心文件/latest/`;
- the daily snapshot verifies and the isolated restore canary passes;
- no duplicate backup root is created.

## Alternatives considered

### Keep the hidden backup root and add a Desktop shortcut

This preserves the old storage location but creates two concepts for customers to
understand. It was rejected because the approved requirement is a real Desktop
folder.

### Create separate Desktop folders for Discord and core memory

This is easy to explain per job but fragments one customer's recovery set and makes
support harder. It was rejected in favor of one server-scoped root.

### Use one real server-scoped Desktop root

This is the approved approach. It provides one visible recovery location while
keeping Discord data and core files separated beneath it.

## Out of scope

- Automatically migrating existing customer backup trees.
- Changing Discord cursor, queue, backlog, audit, or reconciliation semantics.
- Expanding core backup beyond root-level Markdown plus `memory/`.
- Creating, enabling, or mutating live customer cron jobs without an explicit
  deployment action.
- Deleting the legacy backup root after a customer migration.
