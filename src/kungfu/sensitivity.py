# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Estimate how sensitive a learning is (0-10), and draw it as a colored bar.

The score is a heuristic for how much care the text needs, *after* redaction. It counts what was
found and replaced (placeholders left in the text, plus the sanitizer's flags), what kind of source
the text came from, and why it was held back, if it was:

  secrets found (even though redacted)                 +4
  a former name (scrubbed)                             +4.5
  a personal identifier: SSN, card, passport, licence,
    NI number, SIN, birthdate... (removed)             +4, more for several
  sensitive personal subjects (gender history, health,
    orientation, religion, immigration, criminal...)   +3, more for several
  sensitive work subjects (salary, interviews...)      +2.5, more for several
  held back as personal, private or about a person     +1 to +3
  people's names redacted ([person])                   up to +2.5
  contact and network details ([email], [ip], hosts)   up to +2 each
  organization / project / your own name redacted      up to +1.5 each
  URLs to unrecognized sites                           up to +1
  what look like unredacted names (Two Capitalized)    up to +1
  baseline by source (personal vault > memory > repo)  +0.3 to +1

A topic's score blends the average and the worst item in it (0.6 x mean + 0.4 x max), so one
risky learning raises the whole topic.
"""

from __future__ import annotations

import colorsys
import os
import re
import sys
from typing import TextIO

from kungfu.model import Learning

BASE = {
    "obsidian": 1.0, "llm-wiki": 0.8, "claude-memory": 1.0,
    "claude-skill": 0.3, "claude-command": 0.3, "claude-agent": 0.3,
}
HELD = [
    (re.compile(r"about a person|a note about a person|page about a person"), 3.0),
    (re.compile(r"private|share: false"), 3.0),
    (re.compile(r"memory about the person"), 2.0),
    (re.compile(r"CLAUDE\.local"), 1.5),
    (re.compile(r"single project"), 1.0),
    (re.compile(r"describes one project"), 0.5),
]
NAME_LIKE = re.compile(r"(?<![#*\w])([A-Z][a-z]{2,})\s([A-Z][a-z]{2,})\b")
NOT_NAMES = {
    "The", "This", "That", "These", "Use", "Always", "Never", "When", "Then", "For", "And", "But", "With", "From",
    "Into", "Before", "After", "Every", "Each", "Run", "Make", "Keep", "Don", "Read", "Write", "Check", "Add",
    "Claude", "Code", "Desktop", "Obsidian", "GitHub", "Git", "Actions", "Pull", "Request", "Requests", "Merge",
    "Python", "Rust", "Node", "Docker", "Terraform", "Open", "Tofu", "Google", "Microsoft", "Apple", "Amazon",
    "Web", "Services", "Feature", "Branches", "Project", "Overview", "Notes", "Note", "Topics", "Daily", "Rules",
    "Why", "How", "What", "Apply", "Step", "Steps", "Known", "Issues", "Testing", "Development", "Workflow",
}


def _count(text: str, token: str) -> int:
    return text.count(token)


def score(it: Learning) -> float:
    text = f"{it.title}\n{it.summary}\n{it.body}"
    flags = it.flags
    s = BASE.get(it.source_type, 0.5)
    if any(f.startswith("secret:") for f in flags) or "[REDACTED" in text:
        s += 4.0
    sensitive = next((f for f in flags if f.startswith("sensitive:")), None)
    if sensitive:
        n = len(sensitive.split(":", 1)[1].split(","))
        s += min(3.5, 2.5 + 0.5 * (n - 1))
    if "former-name" in flags:
        s += 4.5
    ids = [f for f in flags if f.startswith("id:")]
    if ids:
        s += min(5.0, 4.0 + 0.5 * (len(ids) - 1))
    from kungfu.content import WEIGHTS

    cats = {f.split(":", 1)[0] for f in flags if f.split(":", 1)[0] in WEIGHTS}
    if cats:
        s += min(5.0, sum(WEIGHTS[c] for c in cats) - (len(cats) - 1) * 1.0)
    if any("a slur" in f for f in flags):
        s += 2.0
    if "aws-account-id" in flags:
        s += 0.5
    special = [f for f in flags if f.startswith("special:")]
    if special:
        s += min(4.0, 3.0 + 0.5 * (len(special) - 1))
    if it.held:
        s += max((w for pat, w in HELD if pat.search(it.held)), default=0.0)
    s += min(2.5, 0.8 * (_count(text, "[person]") + ("person-link" in flags)))
    s += min(1.5, 0.4 * _count(text, "[name]"))
    s += min(2.0, 0.7 * (_count(text, "[email]") + _count(text, "[phone]")))
    s += min(2.0, 0.6 * sum(_count(text, t) for t in ("[ip]", "[internal-host]", "[internal-url]")))
    s += min(1.5, 0.5 * _count(text, "the organization"))
    s += min(1.5, 0.2 * _count(text, "[project]"))
    s += min(1.0, 0.25 * _count(text, "the user"))
    s += min(1.0, 0.25 * sum(f.startswith("url:") for f in flags))
    plain = re.sub(r"`[^`]*`", "", re.sub(r"```.*?```", "", text, flags=re.S))
    names = {m.group(0) for m in NAME_LIKE.finditer(plain)
             if m.group(1) not in NOT_NAMES and m.group(2) not in NOT_NAMES}
    s += min(1.0, 0.3 * len(names))
    return round(max(0.0, min(10.0, s)), 1)


def topic_score(items: list[Learning]) -> float:
    if not items:
        return 0.0
    scores = [score(i) for i in items]
    return round(0.6 * sum(scores) / len(scores) + 0.4 * max(scores), 1)


# --- drawing -------------------------------------------------------------------------------

EIGHTHS = " ▏▎▍▌▋▊▉"


def color_mode(stream: TextIO = sys.stdout) -> str:
    """'truecolor', '256' or 'none'. Honors NO_COLOR and FORCE_COLOR."""
    if os.environ.get("NO_COLOR"):
        return "none"
    forced = os.environ.get("FORCE_COLOR")
    if not forced and not (hasattr(stream, "isatty") and stream.isatty()):
        return "none"
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") or forced == "3":
        return "truecolor"
    return "256"


def gradient(position: float) -> tuple[int, int, int]:
    """0 -> green, 0.5 -> yellow, 1 -> red (hue 120 to 0 degrees)."""
    p = max(0.0, min(1.0, position))
    r, g, b = colorsys.hsv_to_rgb((1 - p) * 120 / 360, 0.85, 0.92)
    return round(r * 255), round(g * 255), round(b * 255)


def _fg(rgb: tuple[int, int, int], mode: str) -> str:
    if mode == "truecolor":
        return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if mode == "256":
        r, g, b = (round(c / 255 * 5) for c in rgb)
        return f"\x1b[38;5;{16 + 36 * r + 6 * g + b}m"
    return ""


def bar(value: float, width: int = 10, mode: str = "none") -> str:
    """A horizontal bar for a 0-10 value. Each cell takes the color of its position on the scale,
    so low scores stay green and high scores run through yellow into red."""
    value = max(0.0, min(10.0, value))
    filled = value / 10 * width
    out = []
    for i in range(width):
        frac = filled - i
        if frac >= 1:
            ch = "█"
        elif frac > 0:
            ch = EIGHTHS[max(1, int(frac * 8))]
        else:
            ch = "·"
        if mode != "none" and ch != "·":
            out.append(_fg(gradient((i + 0.5) / width), mode) + ch)
        elif mode != "none":
            out.append("\x1b[2m" + ch)  # dim track
        else:
            out.append(ch)
        if mode != "none":
            out.append("\x1b[0m")
    return "".join(out)


def gauge(value: float, mode: str = "none", width: int = 10) -> str:
    """Bar plus the number, e.g. '████▌····· 4.5'."""
    num = f"{value:>4.1f}"
    if mode != "none":
        num = _fg(gradient(value / 10), mode) + num + "\x1b[0m"
    return f"{bar(value, width, mode)} {num}"


# Shown directly above the column of bars, everywhere they appear.
# The label comes first and is short, so it always fits above the bars; the rest may be trimmed.
HEADER = "sensitivity ▾ how much care the text still needs after scrubbing: 0 green … 10 red"

LEGEND = ("sensitivity 0–10: an estimate from what was redacted (secrets, people, contact details, "
          "org and project names), sensitive subjects and the source")
