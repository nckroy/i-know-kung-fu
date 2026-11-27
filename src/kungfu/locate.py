# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Find git repos, Obsidian vaults and LLM wikis anywhere under your home folder and iCloud.

Used by ``kungfu init`` (and ``kungfu locate``) to offer every place worth reading. It only
lists directory names: it never opens a file, so iCloud placeholders aren't downloaded.

Walked: your home folder (minus ``~/Library``), and every container in
``~/Library/Mobile Documents`` (iCloud Drive, Obsidian's iCloud folder, and so on).
Skipped: hidden folders, app and photo/music libraries, virtual machines, caches and dependency
folders (``node_modules``, ``target``, ``.venv`` …). It doesn't descend into a git repo.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

SKIP_NAMES = {
    "Library", "Applications", "Applications (Parallels)", "Pictures", "Music", "Movies", "Public",
    "VirtualBox VMs", "Parallels", "OrbStack", "Calibre Library", "$RECYCLE.BIN", "System Volume Information",
    "node_modules", "bower_components", "vendor", "target", "build", "dist", "out", "__pycache__", "venv",
    ".venv", "site-packages", "Pods", "DerivedData", "Carthage", "_build", "deps", "obj", "bin",
    "pkg",  # ~/go/pkg/mod holds thousands of module checkouts
}
SKIP_SUFFIXES = (
    ".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".bundle", ".framework", ".pvm", ".vmwarevm",
    ".utm", ".fcpbundle", ".imovielibrary", ".lrdata", ".xcarchive", ".xcodeproj", ".xcworkspace", ".logicx",
    ".band", ".sparsebundle", ".noindex",
)
ICLOUD = Path("~/Library/Mobile Documents")


@dataclass
class Found:
    repos: list[Path] = field(default_factory=list)
    vaults: list[Path] = field(default_factory=list)
    wikis: list[Path] = field(default_factory=list)
    truncated: bool = False  # hit the time limit; results are partial
    seconds: float = 0.0

    def repo_folders(self, home: Path | None = None) -> dict[Path, list[Path]]:
        """Folders that hold repos -> their repos.

        A folder counts as a repo folder when repos are a good share of what's in it, it holds
        several, or its name says so (git, src, code, repos, projects…). Otherwise (a repo sitting
        in ~/Documents among hundreds of other folders, or directly in ~) each repo stands for
        itself, so choosing it doesn't mean reading everything around it.
        """
        home = (home or Path.home()).resolve()
        icloud = ICLOUD.expanduser()
        by_parent: dict[Path, list[Path]] = {}
        for repo in self.repos:
            by_parent.setdefault(repo.parent, []).append(repo)
        groups: dict[Path, list[Path]] = {}
        for parent, repos in by_parent.items():
            if parent in (home, icloud) or parent.parent == icloud or not _holds_repos(parent, len(repos)):
                for r in repos:
                    groups[r] = [r]
            else:
                groups[parent] = list(repos)
        # Fold a folder into an enclosing one only when the enclosing one is close enough that
        # scanning it reaches these repos anyway.
        kept: dict[Path, list[Path]] = {}
        for r in sorted(groups, key=lambda p: len(p.parts)):
            outer = next((k for k in kept if k != r and k in r.parents and len(r.parts) - len(k.parts) <= 2
                          and kept[k] != [k]), None)
            if outer:
                kept[outer].extend(groups[r])
            else:
                kept[r] = list(groups[r])
        return dict(sorted(kept.items(), key=lambda kv: str(kv[0]).lower()))


REPO_FOLDER_WORDS = ("git", "src", "source", "code", "repo", "project", "dev", "work", "github", "gitlab")


def _holds_repos(folder: Path, n_repos: int) -> bool:
    if n_repos >= 5 or any(w in folder.name.lower() for w in REPO_FOLDER_WORDS):
        return True
    try:
        subdirs = sum(1 for e in os.scandir(folder) if e.is_dir(follow_symlinks=False) and not e.name.startswith("."))
    except OSError:
        return False
    return subdirs > 0 and n_repos / subdirs >= 0.3


def _is_wiki(names: set[str], path: Path) -> bool:
    if "wiki" in names and (path / "wiki").is_dir() and names & {"raw", "index.md", "log.md"}:
        return True
    return "index.md" in names and "log.md" in names


def locate(roots: list[Path] | None = None, max_depth: int = 7, time_limit: float = 60.0) -> Found:
    home = Path.home()
    if roots is None:
        roots = [home]
        icloud = ICLOUD.expanduser()
        try:
            roots += sorted(p for p in icloud.iterdir() if p.is_dir())
        except OSError:
            pass
    found = Found()
    start = time.monotonic()
    stack: list[tuple[Path, int]] = [(r, 0) for r in reversed(roots)]
    seen: set[Path] = set()
    while stack:
        if time.monotonic() - start > time_limit:
            found.truncated = True
            break
        d, depth = stack.pop()
        if d in seen:
            continue
        seen.add(d)
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue  # unreadable, or macOS privacy protection declined
        names = {e.name for e in entries}
        if ".obsidian" in names:
            found.vaults.append(d)
        if _is_wiki(names, d):
            found.wikis.append(d)
        if ".git" in names and d != home:
            found.repos.append(d)
            continue
        if depth >= max_depth:
            continue
        subdirs = []
        for e in entries:
            n = e.name
            if n.startswith(".") or n in SKIP_NAMES or n.endswith(SKIP_SUFFIXES):
                continue
            if d == home and n == "Library":
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(Path(e.path))
            except OSError:
                continue
        stack.extend((s, depth + 1) for s in sorted(subdirs, reverse=True))
    found.seconds = time.monotonic() - start
    # A wiki viewed in Obsidian is listed as a wiki only.
    found.vaults = [v for v in found.vaults if v not in found.wikis]
    return found
