# ghostty-workspace

Save and restore multi-tab terminal layouts in [Ghostty](https://ghostty.org) — the missing workspace/arrangement feature.

Ghostty added AppleScript support in v1.3 (March 2026), but has no built-in way to save and restore tab layouts. This script reads a YAML config and drives Ghostty's AppleScript API to create tabs, set titles, configure splits, and run startup commands.

## Requirements

- **macOS** (AppleScript-based)
- **Ghostty 1.3+** with AppleScript support (still marked preview)
- **Python 3.9+**
- **PyYAML** — `pip install pyyaml`
- **Accessibility permission** — System Settings → Privacy & Security → Accessibility → enable the app you run the script from (Terminal, Ghostty, etc.)

## Installation

```bash
git clone https://github.com/manonstreet/ghostty-workspace.git
cd ghostty-workspace
pip install pyyaml
```

Copy `example.yaml` to `~/ghostty-workspace.yaml` (or into a project directory) and edit it for your setup. The script checks the current directory first, then `~/`.

## Usage

```bash
# Launch workspace (checks ./ghostty-workspace.yaml then ~/ghostty-workspace.yaml)
python3 ghostty-workspace.py

# Use a different config
python3 ghostty-workspace.py -c ~/work.yaml

# Open only specific tabs
python3 ghostty-workspace.py --tabs code,server

# Validate config without touching Ghostty
python3 ghostty-workspace.py --dry-run

# Debug: print the generated AppleScript
python3 ghostty-workspace.py --print-script

# Force a new window instead of reusing the front one
python3 ghostty-workspace.py --force-new-window
```

## YAML Schema

See [`example.yaml`](example.yaml) for a fully commented reference. Summary:

### `window`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `shell` | string | *(none)* | **Required** at window or tab level. Absolute path to shell. |
| `tab_position` | `prepend` \| `append` | `prepend` | Where workspace tabs are inserted in the tab bar. |
| `reuse_existing_tabs` | bool | `true` | Global default for reusing tabs that match by title. |
| `always_new` | bool | `false` | Always create a new window (same as `--force-new-window`). |

### `tabs[]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `key` | string | *(required)* | Unique identifier for `--tabs` filtering. |
| `title` | string | | Tab title (set via Ghostty's prompt dialog). If omitted, tab keeps Ghostty's default title and `reuse_if_exists` is implicitly `false`. |
| `working_dir` | string | | Starting directory. Supports `~` and `$ENV_VARS`. |
| `command` | string | | Command to run on tab creation. |
| `shell` | string | `window.shell` | Per-tab shell override. |
| `focus` | bool | `false` | Give this tab focus after launch. |
| `reuse_if_exists` | bool | `window.reuse_existing_tabs` | Override the global reuse setting. |
| `enabled` | bool | `true` | Set to `false` to skip without removing from config. |
| `split` | object | | Simple 2-pane split (see below). Mutually exclusive with `panes`/`layout`. |
| `layout` | string | | Multi-pane layout shorthand (see below). Requires `panes` list. |
| `panes` | list \| object | | Multi-pane config: flat list (with `layout`) or tree (without). |

### `tabs[].split` (2-pane)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Enable the split. |
| `direction` | `right` \| `left` \| `up` \| `down` | `right` | Split direction. |
| `ratio` | string | `"70/30"` | Size ratio. Accepts `"70/30"`, `"0.7"`, or `"70"`. |
| `second_pane_command` | string | | Command for the second pane. |
| `second_pane_working_dir` | string | | Working directory for the second pane. |

### Multi-pane layouts (3+ panes)

For tabs that need more than 2 panes, use `layout` + `panes` (shorthand) or a nested `panes` tree. These are mutually exclusive with the `split` key.

#### Layout shorthand (inspired by [gsx](https://github.com/minorole/gsx))

`layout` is a dash-separated string where each number is the column count for that row. Panes are assigned left-to-right, top-to-bottom.

| Notation | Preset name | Result |
|----------|-------------|--------|
| `"2"` | | 2 columns |
| `"3"` | | 3 columns |
| `"1-1"` | `duo` | 2 rows |
| `"1-2"` | `trio` | 1 top, 2 bottom |
| `"2-2"` | `quad` | 2×2 grid |
| `"1-3"` | `dashboard` | 1 top, 3 bottom |
| `"2-2-2"` | | 3 rows × 2 cols |

```yaml
- key: dashboard
  title: Dashboard
  working_dir: ~/app
  layout: "2-2"                  # or "quad"
  panes:
    - command: htop              # top-left
    - command: kubectl get pods  # top-right
    - command: "tail -f app.log" # bottom-left
    - command: lazygit           # bottom-right
```

#### Tree notation (maximum flexibility)

Each node is either a leaf (`command`/`working_dir`) or a split (`direction` + `panes` with 2+ children). Nodes with >2 children are auto-balanced into equal-size binary splits.

```yaml
- key: dev
  title: Dev Layout
  panes:
    direction: right
    ratio: "70/30"
    panes:
      - direction: down            # left: editor + terminal
        ratio: "75/25"
        panes:
          - command: nvim
            working_dir: ~/app
          - working_dir: ~/app
      - direction: down            # right: tests + logs
        panes:
          - command: "npm test --watch"
            working_dir: ~/app
          - command: "tail -f logs/dev.log"
            working_dir: ~/app
```

Result:

```
┌──────────────┬──────────┐
│              │ npm test │
│     nvim     ├──────────┤
│              │ tail log │
├──────────────┤          │
│   terminal   │          │
└──────────────┴──────────┘
```

#### Flat pane list (no layout key)

A `panes` list without `layout` creates side-by-side columns:

```yaml
panes:
  - command: a
  - command: b
  - command: c
```

### Pane leaf properties

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `command` | string | | Command to run in this pane. |
| `working_dir` | string | tab's `working_dir` | Working directory for this pane. |

## Tests

```bash
python3 test_ghostty_workspace.py
```

109 tests covering config parsing, payload generation, multi-pane layouts, AppleScript invariants, and CLI behavior. No macOS, Ghostty, or osascript required to run them.

## Known Caveats

- **AppleScript support is preview.** Ghostty's scripting API may change between releases.
- **Tab ordering uses an action loop.** Ghostty's sdef has no `move tab` command, so tabs are reordered by calling `perform action "move_tab:-1"` repeatedly. This works but adds ~50ms per position moved. Use `tab_position: append` to skip reordering entirely.
- **Tab titles require Accessibility.** Titles are set via Ghostty's `prompt_tab_title` action and System Events UI automation. Without Accessibility permission, tabs are created but titles won't be set.
- **Split resize is approximate.** Horizontal splits are resized by calculating pixel deltas from the window width. The result is close but not pixel-perfect. For multi-pane layouts, resize applies to top-level horizontal splits only.
- **Multi-pane delay.** Each split adds ~250ms of delay. A 4-pane layout takes ~1s extra; a 9-pane grid ~2s.

## License

MIT
