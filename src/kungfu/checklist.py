# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""An inline, keyboard-driven checklist for the terminal.

    ↑/↓ (or k/j) move · space ticks/unticks · a ticks all or none · →/← show or hide details
    + adds your own · Enter accepts · q or Esc cancels

It draws in place below the cursor (no full-screen takeover), scrolls when the list is taller
than the terminal, and leaves a one-line summary behind when it's done. Uses only termios, so it
needs a real terminal; ``available()`` says whether one is attached.
"""

from __future__ import annotations

import builtins
import os
import re
import select as _pyselect
import shutil
import sys
from typing import Callable, Iterator, TextIO

_ORIGINAL_INPUT = builtins.input
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class Aborted(Exception):
    pass


class Meaning:
    """What a tick does, in words, so no list leaves you guessing.

    on/off: the legend under the title ("[x] = scrubbed: replaced with [person]").
    on_tag/off_tag: shown on each row, and flips as you toggle it ("→ [person]" / "left as written").
    on_count/off_count: the running tally ("2 scrubbed · 1 left as written").
    """

    def __init__(self, on: str, off: str, on_tag: str, off_tag: str, on_count: str, off_count: str) -> None:
        self.on, self.off, self.on_tag, self.off_tag = on, off, on_tag, off_tag
        self.on_count, self.off_count = on_count, off_count


TICKED = Meaning("ticked", "not ticked", "", "", "ticked", "not ticked")


class Choice:
    def __init__(self, label: str, detail: str = "", selected: bool = True, value=None, added: bool = False,
                 more: list[str] | None = None):
        self.label, self.detail, self.selected, self.value, self.added = label, detail, selected, value, added
        self.more = more or []  # extra lines shown when the row is expanded


def available(stdin=None, stdout=None) -> bool:
    """A real terminal on both ends, and input() hasn't been replaced (scripts and tests)."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    return (builtins.input is _ORIGINAL_INPUT and hasattr(stdin, "isatty") and stdin.isatty()
            and hasattr(stdout, "isatty") and stdout.isatty() and os.environ.get("TERM") != "dumb")


# --- keys ----------------------------------------------------------------------------------

SEQUENCES = {
    "[A": "up", "[B": "down", "[C": "right", "[D": "left", "OA": "up", "OB": "down", "OC": "right", "OD": "left",
    "[H": "home", "[F": "end", "OH": "home", "OF": "end", "[1~": "home", "[4~": "end", "[5~": "pgup", "[6~": "pgdn",
}
SINGLE = {" ": "space", "\r": "enter", "\n": "enter", "k": "up", "j": "down", "l": "right", "h": "left",
          "a": "all", "+": "add", "e": "edit", "q": "quit", "\x7f": "left", "g": "home", "G": "end", "?": "help"}


def terminal_keys(raw_letters: bool = False) -> Iterator[str]:
    """Yield key names from the terminal in cbreak mode (Ctrl-C still interrupts).

    With ``raw_letters``, letters come through as ``char:x`` instead of list shortcuts."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            ch = os.read(fd, 1).decode(errors="ignore")
            if ch == "\x1b":
                seq = ""
                while _pyselect.select([fd], [], [], 0.03)[0] and len(seq) < 4:
                    seq += os.read(fd, 1).decode(errors="ignore")
                    if seq[-1:].isalpha() or seq.endswith("~"):
                        break
                yield SEQUENCES.get(seq, "esc" if not seq else "")
            elif raw_letters and ch.isalpha():
                yield f"char:{ch}"
            elif ch == "\t":
                yield "right"
            else:
                yield SINGLE.get(ch, "")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class _cooked:
    """Temporarily restore normal line input (echo, editing) while in cbreak mode."""

    def __enter__(self):
        self.saved = None
        try:
            import termios

            fd = sys.stdin.fileno()
            if os.isatty(fd):
                self.saved = termios.tcgetattr(fd)
                attrs = termios.tcgetattr(fd)
                attrs[3] |= termios.ECHO | termios.ICANON
                termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        except (ImportError, OSError, ValueError):
            self.saved = None
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.saved)


# --- drawing -------------------------------------------------------------------------------


def _visible_len(s: str) -> int:
    return len(ANSI.sub("", s))


def fit(s: str, width: int) -> str:
    """Cut a string with color codes to `width` visible characters."""
    if _visible_len(s) <= width:
        return s
    out, n, i = [], 0, 0
    while i < len(s) and n < width - 1:
        m = ANSI.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(s[i])
        n += 1
        i += 1
    return "".join(out) + ("…\x1b[0m" if ANSI.search(s) else "…")


class Checklist:
    HELP = "↑↓ move · space tick · a all/none · → {more} · {add}{edit}enter accept · q cancel"

    def __init__(self, title: str, choices: list[Choice], note: str = "", out: TextIO | None = None,
                 color: bool = True, add: Callable[[str], list[Choice]] | None = None,
                 add_prompt: str = "Add: ", ask: Callable[[str], str] | None = None,
                 on_edit: Callable[[Choice], None] | None = None, more_label: str = "details",
                 meaning: Meaning | None = None, header: str = "") -> None:
        self.title, self.choices, self.note = title, choices, note
        self.out = out or sys.stdout
        self.color = color
        self.add, self.add_prompt = add, add_prompt
        self.on_edit, self.more_label = on_edit, more_label
        self.meaning = meaning or TICKED
        self.header = header  # a column heading, drawn above the details column
        self.ask = ask or (lambda prompt: _ORIGINAL_INPUT(prompt))
        self.cursor = 0
        self.top = 0
        self.expanded: set[int] = set()
        self.drawn = 0
        self.message = ""

    # styles
    def _c(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if self.color else s

    def _size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((100, 30))
        return max(40, size.columns), max(10, size.lines)

    def _lines(self) -> list[str]:
        width, height = self._size()
        m = self.meaning
        lines = [self._c("1", fit(self.title, width))]
        if m is not TICKED:
            lines.append(fit("  " + self._c("32;1", "[x]") + f" = {m.on}", width))
            lines.append(fit(f"  [ ] = {m.off}", width))
        if self.note:
            import textwrap

            lines += [self._c("2", "  " + ln) for ln in textwrap.wrap(self.note, width - 2)]
        if not self.choices:
            lines.append(self._c("2", "  (nothing found)"))
        label_w = min(48, max((len(c.label) for c in self.choices), default=0))
        tag_w = max(len(m.on_tag), len(m.off_tag))
        if self.header and self.choices:
            # Rows are " ❯ [x] label  tag  details": start the heading where the details start.
            indent = 1 + 1 + 1 + 3 + 1 + label_w + 2 + (tag_w + 2 if tag_w else 0)
            lines.append(self._c("2", fit(" " * indent + self.header, width)))
        # Rows, with expanded detail lines, scrolled to keep the cursor visible.
        rows: list[tuple[int, str]] = []
        for i, c in enumerate(self.choices):
            here = i == self.cursor
            box = self._c("32;1", "[x]") if c.selected else "[ ]"
            pointer = self._c("32;1", "❯") if here else " "
            label = c.label if len(c.label) <= 48 else c.label[:47] + "…"
            label = f"{label:<{label_w}}"
            if here:
                label = self._c("1", label)
            added = self._c("2", "  (added)") if c.added else ""
            detail = c.detail if "\x1b" in c.detail else self._c("2", c.detail)
            tag = ""
            if tag_w:
                tag = (self._c("32", f"{m.on_tag:<{tag_w}}") if c.selected
                       else self._c("2", f"{m.off_tag:<{tag_w}}")) + "  "
            rows.append((i, fit(f" {pointer} {box} {label}  {tag}{detail}{added}", width)))
            if i in self.expanded:
                for extra in c.more or ["(no more detail)"]:
                    rows.append((i, self._c("2", fit("        " + extra, width))))
        room = max(3, height - len(lines) - 3)
        first_row = next(n for n, (i, _) in enumerate(rows) if i == self.cursor) if rows else 0
        last_row = max((n for n, (i, _) in enumerate(rows) if i == self.cursor), default=0)
        if first_row < self.top:
            self.top = first_row
        elif last_row >= self.top + room:
            self.top = last_row - room + 1
        self.top = max(0, min(self.top, max(0, len(rows) - room)))
        visible = rows[self.top:self.top + room]
        if self.top > 0:
            lines.append(self._c("2", f"     ↑ {self.top} more"))
        lines += [r for _, r in visible]
        below = len(rows) - (self.top + len(visible))
        if below > 0:
            lines.append(self._c("2", f"     ↓ {below} more"))
        ticked = sum(c.selected for c in self.choices)
        help_line = self.HELP.format(add="+ add · " if self.add else "", edit="e edit · " if self.on_edit else "",
                                     more=self.more_label)
        status = self.message or f"{ticked} {m.on_count} · {len(self.choices) - ticked} {m.off_count}"
        lines.append(fit(self._c("32", f"  {status}") + self._c("2", f"  ·  {help_line}"), width))
        return lines

    def _paint(self, lines: list[str]) -> None:
        buf = []
        if self.drawn:
            buf.append(f"\x1b[{self.drawn}F")
        buf.append("\x1b[J")
        buf.append("\n".join(lines) + "\n")
        self.out.write("".join(buf))
        self.out.flush()
        self.drawn = len(lines)

    def _finish(self, cancelled: bool) -> None:
        m = self.meaning
        picked = [c.label.strip() for c in self.choices if c.selected]
        rest = len(self.choices) - len(picked)
        if cancelled:
            summary = self._c("2", "cancelled; nothing changed")
        else:
            shown = ", ".join(picked[:5]) + (f", +{len(picked) - 5} more" if len(picked) > 5 else "")
            summary = self._c("32", f"{len(picked)} {m.on_count}") + (self._c("2", f": {shown}") if picked else "")
            if rest:
                summary += self._c("2", f" · {rest} {m.off_count}")
        width, _ = self._size()
        self._paint([fit(self._c("1", self.title) + "  " + summary, width)])
        self.out.write("\x1b[?25h")
        self.out.flush()

    def run(self, keys: Iterator[str]) -> list[Choice]:
        self.out.write("\x1b[?25l")
        cancelled = True
        try:
            self._paint(self._lines())
            for key in keys:
                self.message = ""
                n = len(self.choices)
                if key == "enter":
                    cancelled = False
                    return [c for c in self.choices if c.selected]
                if key in ("quit", "esc"):
                    raise Aborted()
                if n and key == "up":
                    self.cursor = (self.cursor - 1) % n
                elif n and key == "down":
                    self.cursor = (self.cursor + 1) % n
                elif key == "home":
                    self.cursor = 0
                elif key == "end":
                    self.cursor = max(0, n - 1)
                elif key == "pgup":
                    self.cursor = max(0, self.cursor - 10)
                elif key == "pgdn":
                    self.cursor = min(max(0, n - 1), self.cursor + 10)
                elif n and key == "space":
                    self.choices[self.cursor].selected = not self.choices[self.cursor].selected
                elif n and key == "all":
                    target = not all(c.selected for c in self.choices)
                    for c in self.choices:
                        c.selected = target
                elif n and key == "right":
                    self.expanded.add(self.cursor)
                elif n and key == "left":
                    self.expanded.discard(self.cursor)
                elif key == "add" and self.add:
                    self._add()
                elif n and key == "edit" and self.on_edit:
                    self._edit()
                elif key == "help":
                    self.message = "space ticks the row under ❯; Enter accepts what's ticked"
                self._paint(self._lines())
            raise Aborted()  # keys ran out
        finally:
            self._finish(cancelled)

    def _edit(self) -> None:
        # Hand the terminal to $EDITOR, then draw the list afresh below.
        self.out.write("\x1b[?25h")
        self.out.flush()
        with _cooked():
            self.on_edit(self.choices[self.cursor])
        self.out.write("\x1b[?25l")
        self.drawn = 0
        self.message = "edited and re-scrubbed"

    def _add(self) -> None:
        # Drop out of the drawing for a normal line of input, then redraw.
        self.out.write("\x1b[?25h")
        self.out.flush()
        try:
            with _cooked():
                text = self.ask(f"  {self.add_prompt}")
        except EOFError:
            text = ""
        self.drawn += 1  # the prompt line
        self.out.write("\x1b[?25l")
        known = {c.label.lower(): i for i, c in enumerate(self.choices)}
        # Anything the add callback prints ("Not found: …") becomes the status line, not stray output.
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            new = self.add(text) if text.strip() else []
        said = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]
        if said:
            self.message = said[-1]
        for c in new:
            if c.label.lower() in known:
                self.choices[known[c.label.lower()]].selected = True
                self.cursor = known[c.label.lower()]
            else:
                c.added = True
                c.selected = True
                self.choices.append(c)
                self.cursor = len(self.choices) - 1


class Chooser:
    """One question, a row of options: ←/→ (or ↑/↓, tab) to move, Enter to accept, or press an
    option's first letter."""

    def __init__(self, question: str, options: list[str], default: int, out: TextIO, color: bool) -> None:
        self.question, self.options, self.index, self.out, self.color = question, options, default, out, color

    def _line(self, final: bool = False) -> str:
        width = shutil.get_terminal_size((100, 30)).columns
        if final:
            ans = self.options[self.index]
            return fit(f"{self.question}  " + (f"\x1b[32m{ans}\x1b[0m" if self.color else ans), width)
        parts = []
        for i, o in enumerate(self.options):
            if i == self.index:
                parts.append(f"\x1b[32;1m❯ {o}\x1b[0m" if self.color else f"❯ {o}")
            else:
                parts.append(f"\x1b[2m  {o}\x1b[0m" if self.color else f"  {o}")
        hint = "\x1b[2m   ←→ choose · enter accept\x1b[0m" if self.color else "   ←→ choose · enter accept"
        return fit(f"{self.question}  " + "  ".join(parts) + hint, width)

    def run(self, keys: Iterator[str], raw_chars: bool = True) -> int:
        self.out.write("\x1b[?25l\r\x1b[2K" + self._line())
        self.out.flush()
        done = False
        try:
            for key in keys:
                if key == "enter":
                    done = True
                    return self.index
                if key in ("quit", "esc"):
                    raise Aborted()
                if key in ("left", "up"):
                    self.index = (self.index - 1) % len(self.options)
                elif key in ("right", "down", "space"):
                    self.index = (self.index + 1) % len(self.options)
                elif key.startswith("char:"):
                    ch = key[5:].lower()
                    hits = [i for i, o in enumerate(self.options) if o.lower().startswith(ch)]
                    if hits:
                        self.index = hits[0]
                        done = True
                        return self.index
                self.out.write("\r\x1b[2K" + self._line())
                self.out.flush()
            raise Aborted()
        finally:
            self.out.write("\r\x1b[2K" + (self._line(final=True) if done else self.question + "  cancelled")
                           + "\n\x1b[?25h")
            self.out.flush()


def choose(question: str, options: list[str], default: int = 0, out: TextIO | None = None,
           keys: Iterator[str] | None = None, color: bool | None = None) -> int:
    """Ask a question with a few answers; return the chosen index."""
    out = out or sys.stdout
    if color is None:
        from kungfu.sensitivity import color_mode

        color = color_mode(out) != "none"
    ch = Chooser(question, options, default, out, color)
    if keys is not None:
        return ch.run(keys)
    gen = terminal_keys(raw_letters=True)
    try:
        return ch.run(gen)
    finally:
        gen.close()


def run(title: str, choices: list[Choice], note: str = "", add=None, add_prompt: str = "Add: ",
        out: TextIO | None = None, keys: Iterator[str] | None = None, color: bool | None = None,
        ask: Callable[[str], str] | None = None, on_edit: Callable[[Choice], None] | None = None,
        more_label: str = "details", meaning: Meaning | None = None, header: str = "") -> list[Choice]:
    out = out or sys.stdout
    if color is None:
        from kungfu.sensitivity import color_mode

        color = color_mode(out) != "none"
    cl = Checklist(title, choices, note=note, out=out, color=color, add=add, add_prompt=add_prompt, ask=ask,
                   on_edit=on_edit, more_label=more_label, meaning=meaning, header=header)
    if keys is not None:
        return cl.run(keys)
    gen = terminal_keys()
    try:
        return cl.run(gen)
    finally:
        gen.close()  # restores the terminal mode
