#!/usr/bin/env python3
"""
ghostty-fastcopy — hint-based text picker overlay, like tmux-fastcopy but for Ghostty.

Reads terminal content from a file (passed as argv[1]), overlays short letter
hints on every match, and lets the user type a hint to copy the matched text
to the clipboard.  Tab enters multi-select mode.
"""
from __future__ import annotations

import curses
import os
import re
import string
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── patterns — mirrors the user's tmux-fastcopy config exactly ───────────────

PATTERNS: list[tuple[str, str]] = [
    ("url",         r"(?i)\b((https?|ftp|file)://[-A-Z0-9+&@#/%?=~_|!:,.;]*[-A-Z0-9+&@#/%=~_|])\b"),
    ("ipv6",        r"\b([0-9a-fA-F]{1,4}:){7}([0-9a-fA-F]{1,4}|:)\b"),
    ("uuid",        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    ("email",       r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b"),
    ("macaddr",     r"(?i)\b([0-9A-F]{2}[:\-]){5}([0-9A-F]{2})\b"),
    ("creditcard",  r"\b(?:\d[ -]*?){13,16}\b"),
    ("phone",       r"\b\+?\d{1,4}?[-.\s]?\(?\d{1,3}?\)?[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}\b"),
    # Absolute and home-relative paths must come before "domain" so that
    # ~/foo/bar.txt is matched as a path, not split into a domain at bar.txt.
    ("path",        r"(?:(?:^|(?<!\w))(?:~(?=/)|/)[\w./\-~@%+=:,]+)"),
    # Relative paths: bare segments separated by slashes — e.g. git status
    # output, cargo paths, npm @scope/pkg.  Require at least one interior
    # slash to avoid matching lone words.
    ("rel_path",    r"(?<![/\w~])(?:\.\.?/)?[A-Za-z_][\w.@+\-]*(?:/[\w.@+:~\-]+)+"),
    ("domain",      r"(?i)\b(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,6}\b"),
    ("ssn",         r"\b\d{3}-\d{2}-\d{4}\b"),
    ("date",        r"\b\d{2}/\d{2}/\d{4}\b"),
    ("time",        r"\b\d{2}:\d{2}:\d{2}\b"),
    ("k8s",         r"\b[a-z0-9\-]+-[a-z0-9]{8,10}-[a-z0-9]{5}\b"),
    ("rust_test",   r"\b[a-z_]+(?:::[a-z_]+)*::[a-z_]+(?:::[a-z_]+)*\b"),
    ("ipv4",        r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b"),
    ("git_hash_full",  r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])"),
    ("git_hash_short", r"(?<![0-9a-fA-F])[0-9a-fA-F]{7,12}(?![0-9a-fA-F])"),
    ("number",      r"\b\d+(?:[._]\d+)?\b"),
]

# ── strip ANSI / invisible chars ─────────────────────────────────────────────

ANSI_RE = re.compile(
    r'\x1b'
    r'(?:'
    r'\[[0-?]*[ -/]*[@-~]'              # CSI
    r'|\][^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC  (BEL or ST)
    r'|P[^\x1b]*(?:\x1b\\|$)'          # DCS
    r'|[@-Z\\-_]'                       # two-char ESC
    r')'
)
# Zero-width joiners, BOM, soft hyphens, etc. that p10k inserts for alignment
INVISIBLE_RE = re.compile(r'[\u200b\u200c\u200d\u2060\ufeff\u00ad]')
TRAILING_JUNK = re.compile(r"[.,;:!?)'\"\\]+$")


def strip_ansi(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = INVISIBLE_RE.sub("", text)
    return text


# ── data ─────────────────────────────────────────────────────────────────────

@dataclass
class Match:
    text: str
    row: int
    col: int
    kind: str


@dataclass
class State:
    hints: List[str]
    hint_map: Dict[str, Match]
    typed: str = ""
    selected: List[str] = field(default_factory=list)
    multi: bool = False
    scroll_offset: int = 0  # first line of content visible on screen


# ── helpers ───────────────────────────────────────────────────────────────────

def wrap_lines(text: str, width: int) -> List[str]:
    result: List[str] = []
    for line in text.splitlines():
        if not line:
            result.append("")
            continue
        while len(line) > width:
            result.append(line[:width])
            line = line[width:]
        result.append(line)
    return result


def find_matches(lines: List[str]) -> List[Match]:
    seen_text: set[str] = set()
    matches: List[Match] = []
    for row, line in enumerate(lines):
        covered: set[int] = set()
        for kind, pat in PATTERNS:
            for m in re.finditer(pat, line):
                text = TRAILING_JUNK.sub("", m.group())
                if not text:
                    continue
                span = set(range(m.start(), m.start() + len(text)))
                if covered and len(span & covered) > len(span) * 0.3:
                    continue
                covered |= span
                if text in seen_text:
                    continue
                seen_text.add(text)
                matches.append(Match(text=text, row=row, col=m.start(), kind=kind))
    return matches


def generate_hints(n: int) -> List[str]:
    alpha = string.ascii_lowercase
    if n <= 26:
        return list(alpha[:n])
    return [a + b for a in alpha for b in alpha][:n]


def build_state(content: str, cols: int, rows: int = 0) -> tuple[List[str], Optional[State]]:
    lines = wrap_lines(content, cols)
    matches = find_matches(lines)
    if not matches:
        return lines, None
    # Assign hints bottom-first: most recent content (end of history) gets
    # the shortest/earliest hints (a, b, c …) because it is most important.
    bottom_first = list(reversed(matches))
    hints = generate_hints(len(bottom_first))
    hint_map = dict(zip(hints, bottom_first))
    # Default scroll position: show the bottom of the content (most recent).
    scroll_offset = max(0, len(lines) - max(rows - 1, 1))
    return lines, State(hints=hints, hint_map=hint_map, scroll_offset=scroll_offset)


# ── colour pairs ─────────────────────────────────────────────────────────────

P_DIM    = 1
P_HINT   = 2
P_TYPED  = 3
P_PICKED = 4


def init_colors() -> None:
    curses.use_default_colors()
    curses.start_color()
    curses.init_pair(P_DIM,    curses.COLOR_WHITE,  -1)
    curses.init_pair(P_HINT,   curses.COLOR_BLACK,  curses.COLOR_YELLOW)
    curses.init_pair(P_TYPED,  curses.COLOR_BLACK,  curses.COLOR_GREEN)
    curses.init_pair(P_PICKED, curses.COLOR_BLACK,  curses.COLOR_CYAN)


# ── drawing ───────────────────────────────────────────────────────────────────

def draw(stdscr: "curses._CursesWindow", lines: List[str], st: State) -> None:
    rows, cols = stdscr.getmaxyx()
    stdscr.erase()

    offset = st.scroll_offset
    visible = lines[offset: offset + rows - 1]
    dim = curses.color_pair(P_DIM) | curses.A_DIM
    for r, line in enumerate(visible):
        try:
            stdscr.addnstr(r, 0, line, cols, dim)
        except curses.error:
            pass

    active = {h: m for h, m in st.hint_map.items() if h.startswith(st.typed)}
    for hint, match in active.items():
        r = match.row - offset   # translate absolute row → screen row
        c = match.col
        if r < 0 or r >= rows - 1 or c >= cols:
            continue
        is_picked = match.text in st.selected
        typed_part = hint[: len(st.typed)]
        rest_part  = hint[len(st.typed):]
        x = c
        if typed_part:
            attr = curses.color_pair(P_PICKED if is_picked else P_TYPED) | curses.A_BOLD
            try:
                stdscr.addnstr(r, x, typed_part, cols - x, attr)
                x += len(typed_part)
            except curses.error:
                pass
        if rest_part and x < cols:
            attr = curses.color_pair(P_PICKED if is_picked else P_HINT) | curses.A_BOLD
            try:
                stdscr.addnstr(r, x, rest_part, cols - x, attr)
            except curses.error:
                pass

    hint_len = len(st.hints[0]) if st.hints else 1
    mode_tag  = " MULTI " if st.multi else ""
    typed_tag = f"  [{st.typed}]" if st.typed else ""
    picked    = f"  +{len(st.selected)} picked" if st.selected else ""
    max_offset = max(0, len(lines) - (rows - 1))
    scroll_tag = f"  ↕{offset}/{max_offset}" if max_offset > 0 else ""
    status = (
        f" ghostty-fastcopy{mode_tag} "
        f"{len(active)}/{len(st.hints)} hints  "
        f"type {hint_len}-char hint | Tab multi | ↑↓ scroll | ESC/q quit"
        f"{typed_tag}{picked}{scroll_tag}"
    )
    try:
        stdscr.addnstr(rows - 1, 0, status.ljust(cols)[:cols], cols, curses.A_REVERSE)
    except curses.error:
        pass
    stdscr.refresh()


# ── main overlay loop ─────────────────────────────────────────────────────────

def run_overlay(stdscr: "curses._CursesWindow", content: str) -> Optional[List[str]]:
    init_colors()
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(100)

    rows, cols = stdscr.getmaxyx()
    lines, st = build_state(content, cols, rows)
    if st is None:
        return None

    while True:
        cur_rows, cur_cols = stdscr.getmaxyx()
        if cur_rows != rows or cur_cols != cols:
            rows, cols = cur_rows, cur_cols
            lines, st_new = build_state(content, cols, rows)
            if st_new is None:
                return None
            st_new.multi = st.multi
            st_new.selected = st.selected
            st = st_new

        draw(stdscr, lines, st)

        if st.typed and st.typed in st.hint_map:
            match_text = st.hint_map[st.typed].text
            if st.multi:
                if match_text not in st.selected:
                    st.selected.append(match_text)
                st.typed = ""
                continue
            else:
                return [match_text]

        try:
            key = stdscr.getkey()
        except curses.error:
            continue

        if key in ("q", "\x1b"):
            return st.selected if st.selected else None
        elif key in ("\n", "\r", "KEY_ENTER"):
            if st.selected:
                return st.selected
            active = {h: m for h, m in st.hint_map.items() if h.startswith(st.typed)}
            if active:
                return [next(iter(active.values())).text]
            return None
        elif key == "\t":
            st.multi = not st.multi
        elif key in ("KEY_BACKSPACE", "\x7f", "\b"):
            st.typed = st.typed[:-1]
        elif key == "KEY_UP":
            st.scroll_offset = max(0, st.scroll_offset - 1)
        elif key == "KEY_DOWN":
            max_offset = max(0, len(lines) - (rows - 1))
            st.scroll_offset = min(max_offset, st.scroll_offset + 1)
        elif len(key) == 1 and key in string.ascii_lowercase:
            candidate = st.typed + key
            if any(h.startswith(candidate) for h in st.hint_map):
                st.typed = candidate


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ghostty-fastcopy.py <content-file>", file=sys.stderr)
        return 1

    with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
        content = strip_ansi(f.read())

    if not content.strip():
        print("ghostty-fastcopy: no content received", file=sys.stderr)
        return 1

    results = curses.wrapper(lambda s: run_overlay(s, content))

    if not results:
        return 0

    payload = "\n".join(TRAILING_JUNK.sub("", r) for r in results)
    subprocess.run(["pbcopy"], input=payload, text=True, check=False)

    label = "copied" if len(results) == 1 else f"copied {len(results)} items"
    for r in results:
        print(f"  {label}: {r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
