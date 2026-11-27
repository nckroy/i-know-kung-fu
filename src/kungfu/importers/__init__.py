# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Writers that load a bundle's learnings into someone's knowledge base.

Imported text is third-party content. Before it is written:
  * anything Obsidian could execute is defused (``dataviewjs`` blocks, Templater tags, raw HTML
    script/iframe/object/embed, ``javascript:`` links);
  * every note is marked ``trust: external`` and carries a visible "imported" callout, so the
    recipient and their LLM know where it came from;
  * likely prompt-injection phrases and leftover secrets are reported as warnings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from kungfu import frontmatter
from kungfu.model import Learning
from kungfu.sanitize import injection_markers, residual_secrets

_DATAVIEWJS = re.compile(r"^(\s*(?:```|~~~)\s*)(dataviewjs|dvjs|js-engine|templater)\b", re.M | re.I)
_INLINE_DVJS = re.compile(r"`\$=")
_TEMPLATER = re.compile(r"<%")
_HTML_ACTIVE = re.compile(r"<(/?)(script|iframe|object|embed|link|meta|style|form)\b", re.I)
_JS_LINK = re.compile(r"\]\(\s*javascript:", re.I)
_EVENT_ATTR = re.compile(r"(<[^>]*?)\son\w+\s*=", re.I)


def neutralize(body: str) -> str:
    body = _DATAVIEWJS.sub(lambda m: f"{m.group(1)}text", body)
    body = _INLINE_DVJS.sub("`\\$=", body)
    body = _TEMPLATER.sub("&lt;%", body)
    body = _HTML_ACTIVE.sub(lambda m: f"&lt;{m.group(1)}{m.group(2)}", body)
    body = _JS_LINK.sub("](#blocked-javascript-link", body)
    body = _EVENT_ATTR.sub(r"\1 data-blocked-attr=", body)
    return body


@dataclass
class Report:
    written: list[Path] = field(default_factory=list)
    updated: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # titles already present
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def warnings_for(it: Learning) -> list[str]:
    out = []
    text = f"{it.title}\n{it.summary}\n{it.body}"
    inj = injection_markers(text)
    if inj:
        out.append(f"“{it.title}” contains instruction-like text ({'; '.join(inj)}); read it before your AI does.")
    sec = residual_secrets(text)
    if sec:
        out.append(f"“{it.title}” appears to contain a secret or personal identifier ({', '.join(sec)}).")
    if "id-mismatch" in it.flags:
        out.append(f"“{it.title}” was modified after the bundle was packed.")
    return out


def existing_ids(root: Path) -> dict[str, Path]:
    """kungfu_id -> file, for notes previously imported under root."""
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for p in root.rglob("*.md"):
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if "kungfu_id" not in head:
            continue
        fm, _ = frontmatter.split(head + "\n")
        kid = fm.get("kungfu_id")
        if kid:
            found[str(kid)] = p
    return found


_BAD_FILENAME = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


def safe_filename(title: str, max_len: int = 100) -> str:
    name = _BAD_FILENAME.sub(" ", title)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:max_len].strip() or "Untitled"


def unique_path(directory: Path, stem: str, taken: set[Path]) -> Path:
    p = directory / f"{stem}.md"
    n = 2
    while p.exists() or p in taken:
        p = directory / f"{stem} ({n}).md"
        n += 1
    taken.add(p)
    return p


def write_text(path: Path, text: str, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
