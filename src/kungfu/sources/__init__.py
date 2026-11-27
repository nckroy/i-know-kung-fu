# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Readers that turn local knowledge stores into Learning candidates."""

from __future__ import annotations

import re
from pathlib import Path

from kungfu.model import Learning

FENCE = re.compile(r"^\s*(```|~~~)")


def read_text(path: Path, limit: int = 2_000_000) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def split_sections(text: str, level: int = 2) -> list[tuple[str, str]]:
    """Split Markdown into (heading, body) pairs at the given heading level, ignoring code fences.

    Text before the first heading comes back with heading "" (the caller decides what to call it).
    """
    marker = "#" * level + " "
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_fence = False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and line.startswith(marker):
            sections.append((line[len(marker):].strip(), []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(b).strip()) for h, b in sections]


def h1(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def strip_h1(text: str) -> str:
    return re.sub(r"\A\s*# [^\n]*\n", "", text, count=1).strip()


def too_short(item: Learning, minimum: int = 40) -> bool:
    return len(re.sub(r"\s+", " ", item.body).strip()) < minimum
