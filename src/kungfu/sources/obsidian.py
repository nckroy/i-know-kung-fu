# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Obsidian vaults.

A vault is mostly private, so notes are opt-in. A note is offered only if it
  * has a share tag (``#kungfu``, ``#share``, ``#shareable``), or ``share: true`` / ``kungfu: true``, or
  * lives in a folder listed in ``include_folders``.
Notes in deny folders (People, Daily, Meetings, ...) are never offered unless that folder is
explicitly included. ``private: true``, ``share: false`` and ``type: person`` always win.

Titles of notes in people folders are collected so those names can be redacted everywhere.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from kungfu import frontmatter
from kungfu.model import Learning
from kungfu.sources import FENCE, read_text, too_short

# A tag is "#word" at the start of a line or after whitespace. "(#anchor)" in a Markdown link, "page#section"
# and "&#123;" are not tags.
INLINE_TAG = re.compile(r"(?:(?<=\s)|^)#([A-Za-z][\w/\-]*)", re.M)
PERSON_TYPES = {"person", "people", "contact", "colleague"}


def is_vault(path: Path) -> bool:
    return (path / ".obsidian").is_dir()


def _rel_parts(path: Path, root: Path) -> tuple[str, ...]:
    return path.relative_to(root).parts[:-1]


def notes(root: Path) -> Iterator[Path]:
    """Markdown notes in the vault, skipping hidden folders and vaults nested inside it."""
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and not (here / d / ".obsidian").is_dir())
        for f in sorted(filenames):
            if f.endswith(".md") and not f.startswith("."):
                yield here / f


def people_names(root: Path, people_folders: list[str]) -> set[str]:
    wanted = {f.lower() for f in people_folders}
    names = set()
    for p in notes(root):
        if any(part.lower() in wanted for part in _rel_parts(p, root)):
            names.add(p.stem)
            continue
        text = read_text(p, limit=200_000) or ""
        fm, _ = frontmatter.split(text)
        if str(fm.get("type", "")).lower() in PERSON_TYPES:
            names.add(p.stem)
    return names


def tags_of(fm: dict, body: str) -> list[str]:
    tags: list[str] = []
    raw = fm.get("tags") or fm.get("tag") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    tags += [str(t).lstrip("#") for t in raw if t]
    in_fence = False
    for line in body.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            tags += INLINE_TAG.findall(re.sub(r"`[^`]*`", "", line))
    return list(dict.fromkeys(tags))


def _truthy(v) -> bool:
    return v is True or str(v).lower() in ("true", "yes", "1")


def scan(
    root: Path,
    include_folders: list[str],
    deny_folders: list[str],
    share_tags: list[str],
    source_type: str = "obsidian",
    stats: dict | None = None,
) -> Iterator[Learning]:
    """Yield opted-in notes. If given, ``stats`` is filled with counts that explain what was skipped."""
    stats = stats if stats is not None else {}
    for k in ("notes", "denied", "untagged", "offered"):
        stats.setdefault(k, 0)
    include = [f.strip("/").lower() for f in include_folders]
    deny = {f.lower() for f in deny_folders}
    share = {t.lower() for t in share_tags}
    for p in notes(root):
        rel_dir = "/".join(_rel_parts(p, root)).lower()
        parts = {x.lower() for x in _rel_parts(p, root)}
        explicitly_included = any(rel_dir == f or rel_dir.startswith(f + "/") for f in include)
        stats["notes"] += 1
        if parts & deny and not explicitly_included:
            stats["denied"] += 1
            continue
        text = read_text(p)
        if text is None:
            continue
        fm, body = frontmatter.split(text)
        tags = tags_of(fm, body)
        tagged = bool(share & {t.lower() for t in tags}) or _truthy(fm.get("share")) or _truthy(fm.get("kungfu"))
        if not (tagged or explicitly_included):
            stats["untagged"] += 1
            continue
        stats["offered"] += 1
        item = Learning(
            title=str(fm.get("title") or p.stem),
            summary=str(fm.get("description") or fm.get("summary") or ""),
            body=body.strip(),
            source_type=source_type,
            kind="note",
            tags=[t for t in tags if t.lower() not in share],
            origin=str(p),
            context=root.name + ("/" + "/".join(_rel_parts(p, root)) if _rel_parts(p, root) else ""),
        )
        if _truthy(fm.get("private")) or fm.get("share") is False or fm.get("kungfu") is False:
            item.held = "marked private / share: false"
        elif str(fm.get("type", "")).lower() in PERSON_TYPES:
            item.held = "a note about a person"
        elif too_short(item):
            item.held = "too short to be useful"
        # Strip Obsidian comments (%% ... %%), which are private by convention.
        item.body = re.sub(r"%%.*?%%", "", item.body, flags=re.S).strip()
        yield item
