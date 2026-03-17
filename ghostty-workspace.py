#!/usr/bin/env python3
"""
Ghostty workspace launcher.

Reads a YAML config and uses embedded AppleScript via osascript to:
- reuse or create a Ghostty window
- create tabs in a defined order
- skip tabs that already exist by title
- optionally create a split in a tab
- resize a right split to an approximate ratio
- set the visible tab title through Ghostty's prompt_tab_title action
- run commands in a configured shell

Requirements:
- macOS
- Ghostty with AppleScript support (1.3+)
- Python 3.9+
- PyYAML (`python3 -m pip install pyyaml`)
- Accessibility enabled for "System Events"

Example:
    ghostty-workspace --config ~/ghostty-workspace.yaml
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, NoReturn, Tuple, Union

try:
    import yaml
except Exception as exc:  # pragma: no cover
    yaml = None
    YAML_IMPORT_ERROR = exc
else:
    YAML_IMPORT_ERROR = None


@dataclass
class SplitConfig:
    enabled: bool = False
    direction: str = "right"
    ratio: str = "70/30"
    second_pane_command: Optional[str] = None
    second_pane_working_dir: Optional[str] = None


@dataclass
class PaneNode:
    """Binary tree node for multi-pane layouts.

    A leaf has command/working_dir (actual terminal pane).
    A split has direction, ratio, and exactly 2 children.
    """
    command: Optional[str] = None
    working_dir: Optional[str] = None
    direction: Optional[str] = None
    ratio: Optional[str] = None
    children: Optional[Tuple[PaneNode, PaneNode]] = None

    @property
    def is_leaf(self) -> bool:
        return self.children is None


@dataclass
class WindowConfig:
    shell: Optional[str] = None
    tab_position: str = "prepend"
    reuse_existing_tabs: bool = True
    always_new: bool = False


@dataclass
class TabConfig:
    key: str
    title: Optional[str]
    working_dir: Optional[str]
    command: Optional[str]
    shell: str
    split: SplitConfig
    focus: bool = False
    reuse_if_exists: bool = True
    pane_tree: Optional[PaneNode] = None


def die(message: str, code: int = 2) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None:
        die(f"PyYAML is required to read YAML config files. Import error: {YAML_IMPORT_ERROR}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        die(f"config not found: {path}")
    except Exception as exc:
        die(f"failed to read config {path}: {exc}")
    if not isinstance(data, dict):
        die("top-level config must be a mapping/object")
    return data


def expand_path(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = os.path.expandvars(value)
    value = os.path.expanduser(value)
    return str(Path(value))


def normalize_ratio(value: str) -> str:
    raw = str(value).strip()
    if "/" in raw:
        left_s, right_s = raw.split("/", 1)
        try:
            left = float(left_s)
            right = float(right_s)
        except ValueError:
            die(f"invalid split ratio: {value!r}")
        total = left + right
        if total <= 0:
            die(f"invalid split ratio: {value!r}")
        left = left / total
        right = right / total
    else:
        try:
            left = float(raw)
        except ValueError:
            die(f"invalid split ratio: {value!r}")
        if left > 1:
            left = left / 100.0
        if not 0 < left < 1:
            die(f"split ratio must be between 0 and 1 or like '70/30', got {value!r}")
        right = 1.0 - left

    if right <= 0 or left <= 0:
        die(f"split ratio must leave room for both panes, got {value!r}")
    return f"{left:.6f}/{right:.6f}"


def parse_split(obj: Any) -> SplitConfig:
    if obj in (None, False):
        return SplitConfig(enabled=False)
    if obj is True:
        return SplitConfig(enabled=True)

    if not isinstance(obj, dict):
        die("split must be a boolean or mapping")

    enabled = bool(obj.get("enabled", True))
    direction = str(obj.get("direction", "right"))
    if direction not in {"right", "left", "up", "down"}:
        die(f"invalid split direction: {direction!r}")

    ratio = normalize_ratio(obj.get("ratio", "70/30"))
    second_cmd = obj.get("second_pane_command")
    if second_cmd is not None:
        second_cmd = str(second_cmd)

    second_wd = obj.get("second_pane_working_dir")
    if second_wd is not None:
        second_wd = expand_path(str(second_wd))

    return SplitConfig(
        enabled=enabled,
        direction=direction,
        ratio=ratio,
        second_pane_command=second_cmd,
        second_pane_working_dir=second_wd,
    )


# ── Multi-pane helpers ────────────────────────────────────────────────────

LAYOUT_PRESETS: Dict[str, str] = {
    "duo": "1-1",
    "trio": "1-2",
    "quad": "2-2",
    "dashboard": "1-3",
}


def _first_leaf(node: PaneNode) -> PaneNode:
    """Return the leftmost/topmost leaf in a pane tree."""
    if node.is_leaf:
        return node
    return _first_leaf(node.children[0])


def _build_balanced_tree(nodes: List[PaneNode], direction: str) -> PaneNode:
    """Build a right-skewed binary tree from N child nodes with equal sizing."""
    if len(nodes) == 1:
        return nodes[0]
    if len(nodes) == 2:
        return PaneNode(
            direction=direction,
            ratio=normalize_ratio("50/50"),
            children=(nodes[0], nodes[1]),
        )
    n = len(nodes)
    left_frac = 1.0 / n
    right_frac = 1.0 - left_frac
    ratio = f"{left_frac:.6f}/{right_frac:.6f}"
    return PaneNode(
        direction=direction,
        ratio=ratio,
        children=(nodes[0], _build_balanced_tree(nodes[1:], direction)),
    )


def parse_pane_tree(obj: Any, default_wd: Optional[str]) -> PaneNode:
    """Parse a nested pane tree definition from YAML."""
    if not isinstance(obj, dict):
        die("pane node must be a mapping")

    if "panes" in obj:
        direction = str(obj.get("direction", "right"))
        if direction not in {"right", "left", "up", "down"}:
            die(f"invalid pane direction: {direction!r}")
        children_raw = obj["panes"]
        if not isinstance(children_raw, list):
            die("'panes' inside a tree node must be a list")
        children = [parse_pane_tree(c, default_wd) for c in children_raw]
        if len(children) == 2:
            ratio = normalize_ratio(obj.get("ratio", "50/50"))
            return PaneNode(direction=direction, ratio=ratio,
                            children=(children[0], children[1]))
        elif len(children) > 2:
            if "ratio" in obj:
                die("'ratio' is not supported with more than 2 children")
            return _build_balanced_tree(children, direction)
        else:
            die("pane split must have at least 2 children")
    else:
        wd = expand_path(str(obj.get("working_dir", ""))) if obj.get("working_dir") else default_wd
        cmd = obj.get("command")
        if cmd is not None:
            cmd = str(cmd)
        return PaneNode(command=cmd, working_dir=wd)


def parse_layout_shorthand(
    layout: str, panes_raw: List[Any], default_wd: Optional[str],
) -> PaneNode:
    """Convert a layout string like '2-2' + flat pane list into a PaneNode tree."""
    layout = LAYOUT_PRESETS.get(layout, layout)
    parts = layout.strip().split("-")
    try:
        row_counts = [int(p) for p in parts]
    except ValueError:
        die(f"invalid layout: {layout!r}")
    total = sum(row_counts)
    if total < 2:
        die(f"layout must define at least 2 panes, got {total}")
    if total != len(panes_raw):
        die(f"layout '{layout}' expects {total} panes, got {len(panes_raw)}")

    leaves: List[PaneNode] = []
    for p in panes_raw:
        if not isinstance(p, dict):
            p = {}
        wd = expand_path(str(p.get("working_dir", ""))) if p.get("working_dir") else default_wd
        cmd = p.get("command")
        if cmd is not None:
            cmd = str(cmd)
        leaves.append(PaneNode(command=cmd, working_dir=wd))

    idx = 0
    row_nodes: List[PaneNode] = []
    for count in row_counts:
        row_panes = leaves[idx:idx + count]
        idx += count
        if count == 1:
            row_nodes.append(row_panes[0])
        else:
            row_nodes.append(_build_balanced_tree(row_panes, "right"))

    if len(row_nodes) == 1:
        return row_nodes[0]
    return _build_balanced_tree(row_nodes, "down")


def flatten_pane_tree(tree: PaneNode) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Flatten a PaneNode tree into linear split operations and leaf configs.

    Returns (split_ops, pane_leaves) where:
      split_ops[i]: {parentIndex, direction, ratio, newPaneWD}
      pane_leaves[i]: {termIndex, paneCmd, paneWD}

    Terminal 0 is the tab's primary terminal (already exists).
    Each split_op creates a new terminal appended to the list.
    """
    ops: List[Dict[str, Any]] = []
    leaves: List[Dict[str, Any]] = []
    next_idx = [1]  # terminal 0 already exists

    def visit(node: PaneNode, term_idx: int) -> None:
        if node.is_leaf:
            leaves.append({
                "termIndex": term_idx,
                "paneCmd": node.command or "",
                "paneWD": node.working_dir or "",
            })
            return

        new_idx = next_idx[0]
        next_idx[0] += 1

        right_first = _first_leaf(node.children[1])
        ops.append({
            "parentIndex": term_idx,
            "direction": node.direction,
            "ratio": node.ratio,
            "newPaneWD": right_first.working_dir or "",
        })

        visit(node.children[0], term_idx)
        visit(node.children[1], new_idx)

    visit(tree, 0)
    return ops, leaves


def parse_window(data: Dict[str, Any]) -> WindowConfig:
    win = data.get("window")
    if win is None:
        return WindowConfig()
    if not isinstance(win, dict):
        die("'window' must be a mapping/object")

    shell = win.get("shell")
    if shell is not None:
        shell = expand_path(str(shell))

    tab_position = str(win.get("tab_position", "prepend"))
    if tab_position not in {"prepend", "append"}:
        die(f"invalid tab_position: {tab_position!r} (must be 'prepend' or 'append')")

    reuse_existing_tabs = bool(win.get("reuse_existing_tabs", True))
    always_new = bool(win.get("always_new", False))

    return WindowConfig(
        shell=shell,
        tab_position=tab_position,
        reuse_existing_tabs=reuse_existing_tabs,
        always_new=always_new,
    )


def parse_tabs(data: Dict[str, Any], window: WindowConfig) -> List[TabConfig]:
    tabs_obj = data.get("tabs")
    if not isinstance(tabs_obj, list) or not tabs_obj:
        die("config must contain a non-empty 'tabs' list")

    seen_keys: set[str] = set()
    titles: set[str] = set()
    tabs: List[TabConfig] = []

    for i, item in enumerate(tabs_obj, start=1):
        if not isinstance(item, dict):
            die(f"tab #{i} must be a mapping/object")

        if not bool(item.get("enabled", True)):
            continue

        key = str(item.get("key", "")).strip()
        if not key:
            die(f"tab #{i} is missing 'key'")
        if key in seen_keys:
            die(f"duplicate tab key: {key!r}")
        seen_keys.add(key)

        title_raw = item.get("title")
        title: Optional[str] = None
        if title_raw is not None:
            title = str(title_raw).strip() or None
        if title is not None:
            if title in titles:
                die(f"duplicate tab title: {title!r}")
            titles.add(title)

        working_dir = item.get("working_dir")
        if working_dir is not None:
            working_dir = expand_path(str(working_dir))

        command = item.get("command")
        if command is not None:
            command = str(command)

        tab_shell = item.get("shell")
        if tab_shell is not None:
            shell = expand_path(str(tab_shell))
        elif window.shell is not None:
            shell = window.shell
        else:
            die(
                f"tab {key!r} has no shell and no window.shell default is set. "
                f"Set 'shell' on this tab or set 'window.shell' in your config."
            )

        split = parse_split(item.get("split", False))
        focus = bool(item.get("focus", False))

        reuse_raw = item.get("reuse_if_exists")
        if title is None:
            reuse_if_exists = False
        elif reuse_raw is not None:
            reuse_if_exists = bool(reuse_raw)
        else:
            reuse_if_exists = window.reuse_existing_tabs

        # ── multi-pane layout ────────────────────────────────────────
        pane_tree: Optional[PaneNode] = None
        layout_str = item.get("layout")
        panes_raw = item.get("panes")

        if layout_str is not None and panes_raw is not None:
            if not isinstance(panes_raw, list):
                die(f"tab {key!r}: 'panes' must be a list when used with 'layout'")
            pane_tree = parse_layout_shorthand(str(layout_str), panes_raw, working_dir)
        elif panes_raw is not None and layout_str is None:
            if isinstance(panes_raw, dict):
                pane_tree = parse_pane_tree(panes_raw, working_dir)
            elif isinstance(panes_raw, list):
                # Flat list without layout → side-by-side columns
                pane_tree = parse_layout_shorthand(
                    str(len(panes_raw)), panes_raw, working_dir,
                )
            else:
                die(f"tab {key!r}: 'panes' must be a mapping or list")

        if pane_tree is not None and split.enabled:
            die(f"tab {key!r}: cannot use both 'split' and 'panes'/'layout'")

        tabs.append(
            TabConfig(
                key=key,
                title=title,
                working_dir=working_dir,
                command=command,
                shell=shell,
                split=split,
                focus=focus,
                reuse_if_exists=reuse_if_exists,
                pane_tree=pane_tree,
            )
        )

    return tabs


def build_payload(
    tabs: List[TabConfig],
    *,
    only_keys: Optional[List[str]],
    force_new_window: bool,
    tab_position: str = "prepend",
    default_shell: str,
) -> Dict[str, Any]:
    selected = tabs
    tab_order = [t.key for t in tabs]
    if only_keys:
        wanted = set(only_keys)
        selected = [t for t in tabs if t.key in wanted]
        missing = wanted - {t.key for t in selected}
        if missing:
            die(f"unknown tab key(s): {', '.join(sorted(missing))}")
        selected = sorted(selected, key=lambda t: tab_order.index(t.key))

    focus_key = next((t.key for t in selected if t.focus), None)
    if focus_key is None and selected:
        focus_key = selected[0].key

    payload_tabs: List[Dict[str, Any]] = []
    for t in selected:
        tab_data: Dict[str, Any] = {
            "key": t.key,
            "title": t.title or "",
            "workingDir": t.working_dir or "",
            "command": t.command or "",
            "shell": t.shell,
            "reuseIfExists": t.reuse_if_exists,
            "split": {
                "enabled": t.split.enabled,
                "direction": t.split.direction,
                "ratio": t.split.ratio,
                "secondPaneCommand": t.split.second_pane_command or "",
                "secondPaneWorkingDir": t.split.second_pane_working_dir or "",
            },
        }

        if t.pane_tree is not None:
            ops, pane_leaves = flatten_pane_tree(t.pane_tree)
            tab_data["hasMultiPane"] = True
            tab_data["paneOps"] = ops
            tab_data["paneLeaves"] = pane_leaves
        else:
            tab_data["hasMultiPane"] = False
            tab_data["paneOps"] = []
            tab_data["paneLeaves"] = []

        payload_tabs.append(tab_data)

    return {
        "tabs": payload_tabs,
        "forceNewWindow": force_new_window,
        "focusKey": focus_key or "",
        "defaultShell": default_shell,
        "tabPosition": tab_position,
    }


APPLE_SCRIPT = r'''
use AppleScript version "2.4"
use scripting additions

on run
    set payloadRecord to __PAYLOAD_LITERAL__

    set tabList to tabs of payloadRecord
    set forceNewWindow to forceNewWindow of payloadRecord
    set desiredFocusKey to focusKey of payloadRecord
    set defaultShell to defaultShell of payloadRecord
    set tabPosition to tabPosition of payloadRecord

    tell application "Ghostty"
        activate
        if forceNewWindow or (count of windows) = 0 then
            set baseCfg to new surface configuration
            set command of baseCfg to defaultShell
            set win to new window with configuration baseCfg
            delay 0.5
        else
            set win to front window
            activate window win
            delay 0.2
        end if
    end tell

    set focusedTabRef to missing value
    set insertIndex to 1

    repeat with tabItem in tabList
        set tabRec to contents of tabItem
        set keyName to key of tabRec
        set titleText to title of tabRec
        set shellPath to shell of tabRec
        set workingDir to workingDir of tabRec
        set startupCmd to command of tabRec
        set reuseFlag to reuseIfExists of tabRec
        set splitRec to split of tabRec

        set tabRef to my ensureTab(win, titleText, shellPath, workingDir, startupCmd, reuseFlag, splitRec, tabRec)

        if tabPosition is "prepend" then
            -- Slide the tab left until it sits at insertIndex.
            -- Uses the Ghostty action "move_tab:-1" which moves the focused tab one step left.
            tell application "Ghostty"
                select tab tabRef
                delay 0.1
                set currentIndex to index of tabRef
                set primaryTerm to focused terminal of tabRef
                repeat while currentIndex > insertIndex
                    perform action "move_tab:-1" on primaryTerm
                    delay 0.05
                    set currentIndex to currentIndex - 1
                end repeat
            end tell
        end if

        if keyName is desiredFocusKey then set focusedTabRef to tabRef
        set insertIndex to insertIndex + 1
    end repeat

    if focusedTabRef is not missing value then
        tell application "Ghostty"
            select tab focusedTabRef
        end tell
    end if
end run

on ensureTab(win, titleText, shellPath, workingDir, startupCmd, reuseFlag, splitRec, tabRec)
    set multiPane to hasMultiPane of tabRec

    tell application "Ghostty"
        set existingTab to missing value
        if reuseFlag then set existingTab to my findTabByTitle(win, titleText)

        if existingTab is not missing value then
            -- Tab already exists: select it and re-send commands to the focused terminal
            select tab existingTab
            delay 0.15
            set termRef to focused terminal of existingTab
            if workingDir is not "" then
                input text ("cd " & workingDir) to termRef
                send key "enter" to termRef
                delay 0.1
            end if
            if startupCmd is not "" then
                input text startupCmd to termRef
                send key "enter" to termRef
                delay 0.1
            end if
            if (not multiPane) and (enabled of splitRec) then my ensureSplit(existingTab, shellPath, workingDir, splitRec)
            return existingTab
        end if

        -- initial working directory is set via surface config (verified: Fish respects it).
        -- initial input carries only the startup command — keeping them separate avoids
        -- Ghostty splitting the string on whitespace if combined into one field.
        set cfg to new surface configuration
        set command of cfg to shellPath
        if workingDir is not "" then set initial working directory of cfg to workingDir
        -- For multi-pane tabs, commands are sent in executeMultiPane, not via initial input
        if (not multiPane) and startupCmd is not "" then set initial input of cfg to startupCmd & return

        set t to new tab in win with configuration cfg
        delay 0.4
        select tab t
        delay 0.15
    end tell

    -- Set the tab title via the prompt dialog (skip if no title configured)
    if titleText is not "" then my setSelectedTabTitle(titleText)

    -- Re-fetch the tab by its new title: Ghostty may update the internal tab ID
    -- when prompt_tab_title completes, making the original reference stale.
    tell application "Ghostty"
        set t to my findTabByTitle(win, titleText)
        if t is missing value then
            set t to item -1 of (tabs of win as list)
        end if
    end tell

    -- Handle pane layouts
    if multiPane then
        my executeMultiPane(t, shellPath, paneOps of tabRec, paneLeaves of tabRec)
    else if (enabled of splitRec) then
        tell application "Ghostty"
            my ensureSplit(t, shellPath, workingDir, splitRec)
        end tell
    end if

    return t
end ensureTab

on ensureSplit(tabRef, shellPath, workingDir, splitRec)
    tell application "Ghostty"
        if (count of terminals of tabRef) > 1 then return

        select tab tabRef
        delay 0.15
        set primaryTerm to focused terminal of tabRef

        set secondWD to secondPaneWorkingDir of splitRec
        set splitWD to secondWD
        if splitWD is "" then set splitWD to workingDir

        set splitCmd to secondPaneCommand of splitRec

        set splitCfg to new surface configuration
        set command of splitCfg to shellPath
        if splitWD is not "" then set initial working directory of splitCfg to splitWD
        if splitCmd is not "" then set initial input of splitCfg to splitCmd & return

        set directionName to direction of splitRec
        if directionName is "right" then
            set splitTerm to split primaryTerm direction right with configuration splitCfg
        else if directionName is "left" then
            set splitTerm to split primaryTerm direction left with configuration splitCfg
        else if directionName is "up" then
            set splitTerm to split primaryTerm direction up with configuration splitCfg
        else
            set splitTerm to split primaryTerm direction down with configuration splitCfg
        end if

        delay 0.25

        if (directionName is "right") or (directionName is "left") then
            set px to my splitResizePixels(ratio of splitRec, directionName)
            perform action ("resize_split:" & directionName & "," & px) on primaryTerm
        end if

        focus primaryTerm
    end tell
end ensureSplit

on executeMultiPane(tabRef, shellPath, paneOps, paneLeaves)
    tell application "Ghostty"
        select tab tabRef
        delay 0.15

        -- termList: index 1 = the tab's initial terminal, grows with each split
        set termList to {focused terminal of tabRef}

        -- Phase 1: execute all split operations, building up termList
        repeat with opItem in paneOps
            set opRec to contents of opItem
            set parentIdx to (parentIndex of opRec) + 1
            set parentTerm to item parentIdx of termList
            set splitDir to direction of opRec
            set newPaneWD to newPaneWD of opRec

            set splitCfg to new surface configuration
            set command of splitCfg to shellPath
            if newPaneWD is not "" then set initial working directory of splitCfg to newPaneWD

            if splitDir is "right" then
                set newTerm to split parentTerm direction right with configuration splitCfg
            else if splitDir is "left" then
                set newTerm to split parentTerm direction left with configuration splitCfg
            else if splitDir is "down" then
                set newTerm to split parentTerm direction down with configuration splitCfg
            else
                set newTerm to split parentTerm direction up with configuration splitCfg
            end if

            delay 0.25

            -- Resize horizontal splits
            if (splitDir is "right") or (splitDir is "left") then
                set px to my splitResizePixels(ratio of opRec, splitDir)
                perform action ("resize_split:" & splitDir & "," & px) on parentTerm
            end if

            set end of termList to newTerm
        end repeat

        -- Phase 2: send working directory + commands to each leaf pane
        repeat with leafItem in paneLeaves
            set leafRec to contents of leafItem
            set leafIdx to (termIndex of leafRec) + 1
            set leafTerm to item leafIdx of termList
            set leafWD to paneWD of leafRec
            set leafCmd to paneCmd of leafRec

            -- For panes beyond the first, cd to their working dir
            -- (the first pane inherited WD from tab creation; split panes got WD from surface config)
            if leafIdx > 1 and leafWD is not "" then
                focus leafTerm
                delay 0.1
                input text ("cd " & leafWD) to leafTerm
                send key "enter" to leafTerm
                delay 0.1
            end if

            if leafCmd is not "" then
                focus leafTerm
                delay 0.1
                input text leafCmd to leafTerm
                send key "enter" to leafTerm
                delay 0.1
            end if
        end repeat

        -- Return focus to the first pane
        focus (item 1 of termList)
    end tell
end executeMultiPane

on findTabByTitle(win, desiredTitle)
    tell application "Ghostty"
        repeat with t in tabs of win
            try
                if (name of t) is desiredTitle then return t
            end try
        end repeat
    end tell
    return missing value
end findTabByTitle

on setSelectedTabTitle(newTitle)
    -- Open the "Change Tab Title" dialog via the View menu.
    -- prompt_tab_title via perform action returns true but never shows a dialog
    -- (Ghostty 1.3.x bug); clicking the menu item is reliable.
    tell application "System Events"
        tell process "Ghostty"
            click menu item "Change Tab Title..." of menu "View" of menu bar item "View" of menu bar 1
        end tell
    end tell

    -- Wait up to 2 s for the dialog window to appear (count goes from 1 to 2)
    set waited to 0
    repeat while waited < 20
        delay 0.1
        set waited to waited + 1
        try
            tell application "System Events"
                tell process "Ghostty"
                    if (count of windows) > 1 then exit repeat
                end tell
            end tell
        end try
    end repeat

    -- Type the title and confirm; keystrokes go to the focused dialog window
    tell application "System Events"
        keystroke newTitle
        delay 0.05
        keystroke return
    end tell

    delay 0.15
end setSelectedTabTitle

on splitResizePixels(ratioText, directionName)
    set AppleScript's text item delimiters to "/"
    set ratioParts to text items of ratioText
    set AppleScript's text item delimiters to ""

    if (count of ratioParts) is not 2 then error "Invalid ratio: " & ratioText
    set leftFraction to (item 1 of ratioParts) as real
    set rightFraction to (item 2 of ratioParts) as real

    try
        tell application "System Events"
            tell process "Ghostty"
                set winSize to size of front window
            end tell
        end tell

        set winWidth to item 1 of winSize
        set initialHalf to (winWidth * 0.50) as integer

        if directionName is "right" then
            set targetOtherPane to (winWidth * rightFraction) as integer
        else
            set targetOtherPane to (winWidth * leftFraction) as integer
        end if

        set delta to initialHalf - targetOtherPane
        if delta < 80 then set delta to 80
        return delta
    on error
        return 220
    end try
end splitResizePixels
'''


def to_applescript(value: Any) -> str:
    """Recursively convert a Python value to an AppleScript literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        items = ", ".join(to_applescript(v) for v in value)
        return "{" + items + "}"
    if isinstance(value, dict):
        pairs = ", ".join(f"{k}:{to_applescript(v)}" for k, v in value.items())
        return "{" + pairs + "}"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def run_osascript(payload: Dict[str, Any], *, verbose: bool) -> int:
    as_literal = to_applescript(payload)
    cmd = ["osascript", "-"]
    # Inject the payload literal directly into the script so AppleScript
    # never has to parse JSON — avoiding the broken heredoc approach.
    script = APPLE_SCRIPT.replace("__PAYLOAD_LITERAL__", as_literal)

    if verbose:
        print("+", " ".join(shlex.quote(part) for part in cmd), file=sys.stderr)

    proc = subprocess.run(
        cmd,
        input=script,
        text=True,
        capture_output=True,
    )

    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        stderr = proc.stderr.strip()
        if "assistive access" in stderr:
            print(
                "error: Accessibility permission required to set tab titles.\n"
                "  Go to System Settings -> Privacy & Security -> Accessibility\n"
                "  and enable the app you are running this script from (e.g. Terminal, Ghostty).",
                file=sys.stderr,
            )
        else:
            print(stderr, file=sys.stderr)
    return proc.returncode


CONFIG_NAME = "ghostty-workspace.yaml"


def resolve_config() -> Path:
    """Find config: ./ghostty-workspace.yaml first, then ~/ghostty-workspace.yaml."""
    cwd = Path.cwd() / CONFIG_NAME
    if cwd.is_file():
        return cwd
    home = Path.home() / CONFIG_NAME
    if home.is_file():
        return home
    # Default to CWD path so the error message is useful.
    return cwd


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Open and maintain a Ghostty workspace from YAML.")
    p.add_argument(
        "-c", "--config",
        default=None,
        help=f"Path to YAML config. Default: ./{CONFIG_NAME} then ~/{CONFIG_NAME}",
    )
    p.add_argument(
        "--tabs",
        help="Comma-separated list of tab keys to open, in configured order.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and print planned actions without touching Ghostty.",
    )
    p.add_argument(
        "--force-new-window",
        action="store_true",
        help="Always create a new Ghostty window instead of reusing the front window.",
    )
    p.add_argument(
        "--print-script",
        action="store_true",
        help="Print the generated AppleScript and exit (useful for debugging).",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print the osascript command before execution.",
    )
    return p


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.config is not None:
        config_path = Path(expand_path(args.config))
    else:
        config_path = resolve_config()
    data = load_yaml(config_path)
    window_config = parse_window(data)
    tabs = parse_tabs(data, window_config)

    only_keys = None
    if args.tabs:
        only_keys = [part.strip() for part in args.tabs.split(",") if part.strip()]
        if not only_keys:
            die("--tabs was provided but no valid keys were found")

    force_new = args.force_new_window or window_config.always_new

    if args.dry_run:
        selected = tabs
        if only_keys:
            wanted = set(only_keys)
            selected = [t for t in tabs if t.key in wanted]
        print(f"config: {config_path}")
        print(f"window: {'new' if force_new else 'reuse front'}  shell={window_config.shell or '(per-tab)'}  tabs={window_config.tab_position}")
        for t in selected:
            if t.pane_tree is not None:
                ops, pane_leaves = flatten_pane_tree(t.pane_tree)
                split_info = f"  panes={len(pane_leaves)} splits={len(ops)}"
            elif t.split.enabled:
                split_info = f"  split={t.split.direction} {t.split.ratio}"
            else:
                split_info = ""
            focus_marker = " [focus]" if t.focus else ""
            title_display = repr(t.title) if t.title else "(untitled)"
            print(f"  tab {t.key!r}: {title_display}  cmd={t.command or '(none)'}{split_info}{focus_marker}")
        return 0

    if not tabs:
        print("no enabled tabs to open")
        return 0

    # Resolve the default shell for initial window creation.
    # Use window.shell if set, otherwise use the first tab's shell.
    default_shell = window_config.shell
    if default_shell is None:
        default_shell = tabs[0].shell

    payload = build_payload(
        tabs,
        only_keys=only_keys,
        force_new_window=force_new,
        tab_position=window_config.tab_position,
        default_shell=default_shell,
    )

    if args.print_script:
        as_literal = to_applescript(payload)
        print(APPLE_SCRIPT.replace("__PAYLOAD_LITERAL__", as_literal))
        return 0

    return run_osascript(payload, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
