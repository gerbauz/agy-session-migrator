# Antigravity Session Migrator

A lightweight, zero-dependency interactive CLI tool to migrate chat and agent sessions from **Antigravity CLI** (`agy`) into **Antigravity Desktop** across Windows, macOS, and Linux.

---

## Features

- **Cross-Platform**: Runs natively on Windows, macOS, and Linux using Python standard library only.
- **Interactive TUI**: Smooth terminal interface with arrow key navigation, pagination, and multi-session selection.
- **Automatic Subagent Grouping**: Child subagent trajectories are automatically bundled with their parent sessions.
- **Live Language Server Sync**: Automatically syncs sessions with the running Antigravity Desktop language server and updates backing SQLite summaries.
- **Safety First**:
  - Non-destructive: original Antigravity CLI files are never modified or removed.
  - Automatically prunes ghost/mirrored session entries from the CLI database when opening a project.
  - Prevents opening 0-session projects and provides clear status alerts.

---

## Prerequisites

- **Python**: 3.8 or higher.
- **Antigravity CLI** (`agy`): Installed and authenticated.
- **Antigravity Desktop**: Installed and running during migration.
- **Zero Third-Party Dependencies**: Uses Python standard library only.

---

## Usage

Ensure **Antigravity Desktop** is launched and running, then run the migrator from your terminal:

```bash
python migrate_agy_sessions.py
```

---

## Navigation & Controls

### Step 1: Select Target Project
- <kbd>↑</kbd> / <kbd>↓</kbd> or <kbd>k</kbd> / <kbd>j</kbd>: Move selection cursor
- <kbd>Page Up</kbd> / <kbd>Page Down</kbd>: Scroll page
- <kbd>Enter</kbd>: Open selected project (or exit if on `[ < Exit Application ]`)
- <kbd>Esc</kbd> or <kbd>q</kbd>: Exit application

### Step 2: Select Sessions to Migrate
- <kbd>↑</kbd> / <kbd>↓</kbd>: Move selection cursor
- <kbd>Space</kbd> or <kbd>Enter</kbd>: Toggle checkmark on highlighted session
- <kbd>a</kbd>: Toggle select all visible sessions
- <kbd>f</kbd>: Toggle filter by current project / show all
- <kbd>c</kbd>: Confirm and start migration
- <kbd>Esc</kbd> or <kbd>b</kbd>: Return to Project Selection
- <kbd>q</kbd>: Quit application

---

## Optional Arguments

By default, the tool resolves standard paths (`~/.gemini/antigravity-cli`, `~/.gemini/antigravity`, `~/.gemini/config`). You can override them if needed:

```bash
python3 migrate_agy_sessions.py --cli-dir /custom/path/antigravity-cli --app-dir /custom/path/antigravity --config-dir /custom/path/config
```

| Flag | Description | Default |
|---|---|---|
| `--cli-dir` | Path to Antigravity CLI directory | `~/.gemini/antigravity-cli` |
| `--app-dir` | Path to Antigravity Desktop directory | `~/.gemini/antigravity` |
| `--config-dir` | Path to Antigravity config directory | `~/.gemini/config` |

---

## Running Automated Tests

To run the automated test suite:

```bash
pytest
```

