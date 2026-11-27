# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""The unit everything else passes around: one learning."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

KINDS = ("lesson", "practice", "gotcha", "convention", "preference", "reference", "howto", "skill", "note")
# Structured fields a lesson carries in its front matter, besides title/kind/topics/summary.
LESSON_FIELDS = ("statement", "confidence", "applies_when", "not_when", "evidence")


@dataclass
class Learning:
    title: str
    body: str
    source_type: str  # claude-memory, claude-md, agents-md, cursor-rules, claude-skill, obsidian, llm-wiki, ...
    kind: str = "note"
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # raw source tags, used for topic inference
    # Local-only fields. These are shown to the person reviewing an export and are never written to a bundle.
    origin: str = ""  # path on disk
    context: str = ""  # e.g. the repo or memory project it came from
    flags: list[str] = field(default_factory=list)  # sanitizer findings, for the reviewer
    held: str | None = None  # why it is excluded by default; None means it is offered for export
    meta: dict = field(default_factory=dict)  # lesson fields (LESSON_FIELDS), written to the bundle

    @property
    def id(self) -> str:
        norm = re.sub(r"\s+", " ", f"{self.title}\n{self.body}".strip().lower())
        return hashlib.sha256(norm.encode()).hexdigest()[:16]

    def flag(self, name: str) -> None:
        if name not in self.flags:
            self.flags.append(name)


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (text[:max_len].rstrip("-")) or "untitled"


def humanize_slug(slug: str) -> str:
    words = re.split(r"[-_]+", slug.strip())
    s = " ".join(w for w in words if w)
    return s[:1].upper() + s[1:] if s else slug
