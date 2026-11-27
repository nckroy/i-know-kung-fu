# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Agent instruction files: the global ``~/.claude/CLAUDE.md`` and, in every git repo under the
configured roots, CLAUDE.md / AGENTS.md / GEMINI.md / Cursor, Copilot, Windsurf and Cline rules,
plus Claude Code skills, slash commands and subagents.

Instruction files are split at ``##`` headings so each section can be chosen on its own. Skills,
commands and agents are kept whole.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator

from kungfu import frontmatter
from kungfu.model import Learning, humanize_slug
from kungfu.sources import h1, read_text, split_sections, strip_h1, too_short

# file (relative to repo root) -> source type
SECTIONED = {
    "CLAUDE.md": "claude-md",
    ".claude/CLAUDE.md": "claude-md",
    "AGENTS.md": "agents-md",
    "GEMINI.md": "gemini-md",
    "CONVENTIONS.md": "conventions-md",
    ".cursorrules": "cursor-rules",
    ".windsurfrules": "windsurf-rules",
    ".clinerules": "cline-rules",
    ".github/copilot-instructions.md": "copilot-instructions",
}
PRIVATE = {"CLAUDE.local.md": "claude-md"}
WHOLE_GLOBS = {
    ".claude/skills/*/SKILL.md": "claude-skill",
    ".claude/commands/*.md": "claude-command",
    ".claude/agents/*.md": "claude-agent",
    ".cursor/rules/*.mdc": "cursor-rules",
    ".github/instructions/*.instructions.md": "copilot-instructions",
}
# Sections that describe one codebase rather than teach something portable. Held back by default;
# `kungfu export --include-held` lets you pick them anyway.
PROJECT_HEADING = re.compile(
    r"(?i)^(project )?(overview|summary|description|introduction|about)$|repository (layout|structure|information|info)|"
    r"(directory|folder|project|file|code) (structure|layout|map|organi[sz]ation)|^architecture( overview)?$|"
    r"api endpoints?|^endpoints$|^configuration$|^(dependencies|tech(nology)? stack|stack)$|"
    r"(build|development|common|useful|key) (& development )?commands|^commands$|key implementation details|"
    r"^known issues$|^still open$|^(status|roadmap|todo|to-do|next steps)$|^environment variables$"
)
KIND_BY_SOURCE = {"claude-skill": "skill", "claude-command": "howto", "claude-agent": "howto"}

SKIP_DIRS = {
    "node_modules", ".venv", "venv", "target", "vendor", "dist", "build", "__pycache__", "Library",
    ".git", ".cache", ".terraform", "Pods", "DerivedData",
}


def find_repos(roots: list[Path], max_depth: int = 4) -> list[Path]:
    repos: list[Path] = []
    for root in roots:
        root = root.expanduser()
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root):
            p = Path(dirpath)
            if (p / ".git").exists():
                repos.append(p)
                dirnames[:] = []  # don't descend into a repo looking for more repos
                continue
            if len(p.parts) - base_depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
    return sorted(set(repos))


_REMOTE = re.compile(r"url\s*=\s*(?:\w+://)?(?:[^@/\s]+@)?([^:/\s]+)[:/]+([^/\s]+)/", re.I)
_EMAIL = re.compile(r"email\s*=\s*(\S+)@(\S+)", re.I)


def repo_identity(repo: Path) -> tuple[set[str], set[str]]:
    """(owner of the origin remote, repo-local email domains), read from .git/config.

    Only ``origin`` counts: other remotes (a fork's ``upstream``) belong to someone else.
    """
    cfg = repo / ".git" / "config"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(), set()
    owners: set[str] = set()
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            continue
        if section == '[remote "origin"]':
            m = _REMOTE.search(stripped)
            if m:
                owners.add(m.group(2))
    domains = {m.group(2).lower() for m in _EMAIL.finditer(text)}
    return owners, domains


def repo_emails(repo: Path) -> set[str]:
    """Your repo-local commit addresses (user.email in .git/config), lowercased."""
    try:
        text = (repo / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {f"{m.group(1)}@{m.group(2)}".lower() for m in _EMAIL.finditer(text)}


_AUTHORS: dict[str, list[tuple[str, str]]] = {}


def git_authors(repo: Path, limit: int = 400) -> list[tuple[str, str]]:
    """(name, lowercased email) for recent commits. Empty for a repo with no commits."""
    key = str(repo)
    if key not in _AUTHORS:
        import subprocess

        try:
            out = subprocess.run(["git", "-C", key, "log", f"-n{limit}", "--format=%an%x09%ae"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        pairs = []
        for line in out.splitlines():
            name, _, email = line.partition("\t")
            pairs.append((name.strip(), email.strip().lower()))
        _AUTHORS[key] = pairs
    return _AUTHORS[key]


def my_repos(repos: list[Path], identities: set[str], former=None) -> list[Path]:
    """Repos you've committed to, have no history yet, or share an owner with one of those.

    Third-party clones (someone else's project you checked out) don't hold your learnings, and
    their owners and names aren't yours to redact.
    """
    if former is None:
        from kungfu import former_names

        former = former_names.current()
    ids = {i.lower() for i in identities}
    for r in repos:
        ids |= repo_emails(r)
    handles = {i for i in ids if "@" not in i and " " not in i}

    def authored(repo: Path) -> bool:
        _, local_emails = repo_identity(repo)
        authors = git_authors(repo)
        if not authors:
            return True
        for name, email in authors:
            if (name.lower() in ids or email in ids or email.split("@")[0] in ids
                    or any(f"+{h}@" in email or email.startswith(f"{h}@") for h in handles)
                    or (former and (former.matches(name) or former.matches(email)
                                    or former.matches(email.split("@")[0])))):
                return True
        return False

    mine = [r for r in repos if authored(r)]
    owners = set()
    for r in mine:
        owners |= {o.lower() for o in repo_identity(r)[0]}
    owners |= handles
    rest = [r for r in repos if r not in mine and {o.lower() for o in repo_identity(r)[0]} & owners]
    return sorted(mine + rest)


def nested_instruction_files(repo: Path, max_depth: int = 3) -> list[Path]:
    """CLAUDE.md / AGENTS.md files in subdirectories (monorepo packages)."""
    found = []
    base = len(repo.parts)
    for dirpath, dirnames, filenames in os.walk(repo):
        p = Path(dirpath)
        depth = len(p.parts) - base
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if depth >= max_depth:
            dirnames[:] = []
        if depth == 0:
            continue
        for name in ("CLAUDE.md", "AGENTS.md"):
            if name in filenames:
                found.append(p / name)
    return found


def sectioned(path: Path, source_type: str, context: str) -> Iterator[Learning]:
    text = read_text(path)
    if text is None:
        return
    fm, text = frontmatter.split(text)
    doc_title = h1(text) or path.name
    for heading, body in split_sections(text, 2):
        if not heading:
            body = strip_h1(body)
            heading = doc_title
        item = Learning(
            title=heading,
            body=body,
            source_type=source_type,
            kind="convention",
            origin=str(path),
            context=context,
        )
        if too_short(item, 80 if heading == doc_title else 40):
            continue
        if PROJECT_HEADING.search(heading.strip(" #*`:")):
            item.held = "describes one project (overview, layout, commands...) rather than a portable lesson"
        yield item


def whole(path: Path, source_type: str, context: str) -> Iterator[Learning]:
    text = read_text(path)
    if text is None:
        return
    fm, body = frontmatter.split(text)
    name = str(fm.get("name") or (path.parent.name if path.name == "SKILL.md" else path.stem))
    item = Learning(
        title=humanize_slug(name) if "-" in name or "_" in name else name,
        summary=str(fm.get("description") or ""),
        body=body.strip(),
        source_type=source_type,
        kind=KIND_BY_SOURCE.get(source_type, "convention"),
        origin=str(path),
        context=context,
    )
    if not too_short(item):
        yield item


def scan_repo(repo: Path) -> Iterator[Learning]:
    ctx = repo.name
    for rel, stype in SECTIONED.items():
        p = repo / rel
        if p.is_file():
            yield from sectioned(p, stype, ctx)
    for rel, stype in PRIVATE.items():
        p = repo / rel
        if p.is_file():
            for item in sectioned(p, stype, ctx):
                item.held = "from CLAUDE.local.md, which is personal and not checked in"
                yield item
    for pattern, stype in WHOLE_GLOBS.items():
        for p in sorted(repo.glob(pattern)):
            yield from whole(p, stype, ctx)
    for p in nested_instruction_files(repo):
        yield from sectioned(p, SECTIONED.get(p.name, "claude-md"), f"{ctx}/{p.parent.relative_to(repo)}")


def scan_global(claude_dir: Path) -> Iterator[Learning]:
    p = claude_dir / "CLAUDE.md"
    if p.is_file():
        yield from sectioned(p, "claude-md", "global")
    for pattern, stype in (("skills/*/SKILL.md", "claude-skill"), ("commands/*.md", "claude-command"),
                           ("agents/*.md", "claude-agent")):
        for f in sorted(claude_dir.glob(pattern)):
            yield from whole(f, stype, "global")


def repos_to_read(repo_roots: list[Path], only_mine: bool = True) -> list[Path]:
    repos = find_repos(repo_roots)
    if not only_mine:
        return repos
    from kungfu.config import user_identities

    return my_repos(repos, user_identities())


def scan(claude_dir: Path | None, repo_roots: list[Path], only_mine: bool = True) -> Iterator[Learning]:
    if claude_dir and claude_dir.is_dir():
        yield from scan_global(claude_dir)
    for repo in repos_to_read(repo_roots, only_mine):
        yield from scan_repo(repo)
