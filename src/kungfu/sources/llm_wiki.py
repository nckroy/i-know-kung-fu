# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Karpathy-style LLM wikis.

The pattern: ``raw/`` holds immutable source documents, ``wiki/`` holds pages the LLM writes and
maintains (concepts, entities, summaries), ``index.md`` catalogs the pages and ``log.md`` is an
append-only history. A schema file (CLAUDE.md / AGENTS.md) tells the LLM how to maintain it.

Only ``wiki/`` pages are learnings. ``raw/`` is other people's material and never exported.
Entity pages about people are held back.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from kungfu import frontmatter
from kungfu.model import Learning
from kungfu.sources import read_text, too_short
from kungfu.sources.obsidian import PERSON_TYPES, tags_of

SPECIAL = {"index.md", "log.md", "claude.md", "agents.md", "readme.md", "schema.md", "gemini.md"}
PEOPLE_DIRS = {"people", "persons", "person", "entities/people", "contacts"}


def is_wiki(path: Path) -> bool:
    if (path / "wiki").is_dir() and any((path / n).exists() for n in ("raw", "index.md", "log.md", "wiki/index.md")):
        return True
    return (path / "index.md").is_file() and (path / "log.md").is_file()


def pages_dir(root: Path) -> Path:
    return root / "wiki" if (root / "wiki").is_dir() else root


def people_names(root: Path) -> set[str]:
    names = set()
    base = pages_dir(root)
    for p in base.rglob("*.md"):
        rel = "/".join(p.relative_to(base).parts[:-1]).lower()
        if rel in PEOPLE_DIRS or rel.split("/")[0] in PEOPLE_DIRS:
            names.add(p.stem)
            continue
        fm, _ = frontmatter.split(read_text(p, 200_000) or "")
        if str(fm.get("type", "")).lower() in PERSON_TYPES:
            names.add(p.stem)
    return names


def scan(root: Path) -> Iterator[Learning]:
    base = pages_dir(root)
    for p in sorted(base.rglob("*.md")):
        rel_parts = p.relative_to(base).parts
        if any(x.startswith(".") for x in rel_parts) or rel_parts[0] == "raw":
            continue
        if p.name.lower() in SPECIAL and len(rel_parts) == 1:
            continue
        text = read_text(p)
        if text is None:
            continue
        fm, body = frontmatter.split(text)
        tags = tags_of(fm, body)
        ptype = str(fm.get("type", "")).lower()
        if ptype and ptype not in tags:
            tags.append(ptype)
        item = Learning(
            title=str(fm.get("title") or p.stem),
            summary=str(fm.get("summary") or fm.get("description") or ""),
            body=body.strip(),
            source_type="llm-wiki",
            kind="note",
            tags=[t for t in tags if t.lower() not in ("concept", "entity", "summary", "source", "page")],
            origin=str(p),
            context=root.name,
        )
        dir_rel = "/".join(rel_parts[:-1]).lower()
        if ptype in PERSON_TYPES or dir_rel in PEOPLE_DIRS or (dir_rel and dir_rel.split("/")[0] in PEOPLE_DIRS):
            item.held = "a page about a person"
        elif str(fm.get("private", "")).lower() == "true" or fm.get("share") is False:
            item.held = "marked private / share: false"
        elif too_short(item):
            item.held = "too short to be useful"
        item.body = re.sub(r"%%.*?%%", "", item.body, flags=re.S).strip()
        yield item
