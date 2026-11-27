# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Claude Code auto-memory: ``~/.claude/projects/<project>/memory/*.md``.

Each memory file is one learning. ``feedback`` and ``reference`` memories are offered; ``user``
memories (about the person) and ``project`` memories (state of one project) are held back, as are
TODO notes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from kungfu import frontmatter
from kungfu.model import Learning, humanize_slug
from kungfu.sources import read_text, too_short

KIND_BY_TYPE = {"feedback": "practice", "reference": "reference", "user": "preference", "project": "note"}
HELD_TYPES = {
    "user": "memory about the person (type: user)",
    "project": "state of a single project (type: project)",
}


def project_cwd(project_dir: Path) -> str:
    """Recover the real working directory from a session transcript; the folder name is lossy."""
    for jsonl in sorted(project_dir.glob("*.jsonl"))[:3]:
        try:
            with jsonl.open(encoding="utf-8", errors="replace") as fh:
                for _, line in zip(range(50), fh):
                    if '"cwd"' in line:
                        cwd = json.loads(line).get("cwd")
                        if cwd:
                            return cwd
        except (OSError, ValueError):
            continue
    return ""


def scan(claude_dir: Path) -> Iterator[Learning]:
    for mem_dir in sorted((claude_dir / "projects").glob("*/memory")):
        files = [p for p in sorted(mem_dir.glob("*.md")) if p.name != "MEMORY.md"]
        if not files:
            continue
        cwd = project_cwd(mem_dir.parent)
        context = Path(cwd).name if cwd else mem_dir.parent.name
        for path in files:
            text = read_text(path)
            if text is None:
                continue
            fm, body = frontmatter.split(text)
            meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
            mtype = str(fm.get("type") or meta.get("type") or "").lower()
            name = str(fm.get("name") or path.stem)
            item = Learning(
                title=humanize_slug(name),
                summary=str(fm.get("description") or ""),
                body=body.strip(),
                source_type="claude-memory",
                kind=KIND_BY_TYPE.get(mtype, "note"),
                origin=str(path),
                context=context,
            )
            if mtype in HELD_TYPES:
                item.held = HELD_TYPES[mtype]
            elif name.lower().startswith("todo") or path.stem.lower().startswith("todo"):
                item.held = "a to-do, not a learning"
            elif too_short(item):
                item.held = "too short to be useful"
            yield item


def project_names(claude_dir: Path) -> set[str]:
    """Names of the projects that have memories; used to redact project names."""
    names = set()
    for pdir in (claude_dir / "projects").glob("*"):
        if (pdir / "memory").is_dir():
            cwd = project_cwd(pdir)
            if cwd:
                names.add(Path(cwd).name)
    return names
