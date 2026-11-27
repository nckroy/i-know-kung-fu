#!/usr/bin/env python3
# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Regenerate the README screenshots in this folder.

    python3 docs/screenshots/generate.py

Everything shown is made up: the script builds a demo home folder (Claude Code memories, a repo with
a CLAUDE.md, an Obsidian vault with a People folder) in a temporary directory and runs kungfu's real
code against it, with color forced on. Claude's part (writing lessons, examining them) is replaced
by canned answers, so no API key is needed and nothing is sent anywhere.

Terminal output is converted to SVG. Each run of same-styled text gets a fixed width
(textLength), so columns and bars stay aligned whatever monospace font the viewer has.
"""

from __future__ import annotations

import html
import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

COLS = 120
CELL_W, LINE_H, FONT = 8.4, 18, 14
BG, FG, DIM = "#0d1117", "#c9d1d9", "#6e7681"
BASIC = {30: "#484f58", 31: "#ff7b72", 32: "#3fb950", 33: "#d29922", 34: "#58a6ff", 35: "#bc8cff", 36: "#39c5cf",
         37: "#b1bac4"}


# --- ANSI to SVG -----------------------------------------------------------------------------

def _xterm256(n: int) -> str:
    if n < 16:
        base = ["#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5"]
        return base[n % 8]
    if n < 232:
        n -= 16
        r, g, b = n // 36, (n // 6) % 6, n % 6
        conv = lambda v: 0 if v == 0 else 55 + v * 40  # noqa: E731
        return f"#{conv(r):02x}{conv(g):02x}{conv(b):02x}"
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


def parse(text: str) -> list[list[tuple[str, dict]]]:
    """Lines of (text, style) runs from ANSI output. Cursor movement and screen clearing are dropped."""
    text = re.sub(r"\x1b\[\?25[hl]|\x1b\[\d*[FAJK]|\r", "", text)
    lines, cur, style = [], [], {}
    for part in re.split(r"(\x1b\[[0-9;]*m|\n)", text):
        if part == "\n":
            lines.append(cur)
            cur = []
        elif part.startswith("\x1b["):
            codes = [int(c) for c in part[2:-1].split(";") if c] or [0]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    style = {}
                elif c == 1:
                    style = {**style, "bold": True}
                elif c == 2:
                    style = {**style, "dim": True}
                elif c in BASIC:
                    style = {**style, "fg": BASIC[c]}
                elif c == 38 and codes[i + 1:i + 2] == [2]:
                    style = {**style, "fg": "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])}
                    i += 4
                elif c == 38 and codes[i + 1:i + 2] == [5]:
                    style = {**style, "fg": _xterm256(codes[i + 2])}
                    i += 2
                i += 1
        elif part:
            cur.append((part, dict(style)))
    if cur:
        lines.append(cur)
    while lines and not "".join(t for t, _ in lines[-1]).strip():
        lines.pop()
    return lines


def svg(text: str, title: str, path: Path) -> None:
    lines = parse(text)
    cols = max([COLS] + [sum(len(t) for t, _ in runs) for runs in lines])
    width = int(cols * CELL_W + 32)
    height = int(len(lines) * LINE_H + 56)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           f'<rect width="{width}" height="{height}" rx="8" fill="{BG}"/>',
           '<circle cx="18" cy="16" r="5" fill="#ff5f57"/><circle cx="34" cy="16" r="5" fill="#febc2e"/>'
           '<circle cx="50" cy="16" r="5" fill="#28c840"/>',
           f'<text x="{width / 2}" y="20" fill="{DIM}" font-size="12" text-anchor="middle" '
           f'font-family="-apple-system, Segoe UI, sans-serif">{html.escape(title)}</text>',
           f'<g font-family="Menlo, Consolas, \'DejaVu Sans Mono\', monospace" font-size="{FONT}" xml:space="preserve">']
    for row, runs in enumerate(lines):
        y = 44 + row * LINE_H + 13
        col = 0
        for txt, st in runs:
            # One element per word, each at its own column: renderers treat spaces inside a text
            # element differently, so spaces are never drawn, only skipped.
            for m in re.finditer(r"\S+", txt):
                word = m.group(0)
                fill = st.get("fg", FG)
                attrs = [f'x="{16 + (col + m.start()) * CELL_W:.1f}"', f'y="{y}"', f'fill="{fill}"',
                         f'textLength="{len(word) * CELL_W:.1f}"', 'lengthAdjust="spacingAndGlyphs"']
                if st.get("bold"):
                    attrs.append('font-weight="bold"')
                if st.get("dim"):
                    attrs.append('fill-opacity="0.55"')
                out.append(f"<text {' '.join(attrs)}>{html.escape(word)}</text>")
            col += len(txt)
    out.append("</g></svg>")
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)} ({cols} columns, {len(lines)} lines)")


# --- a made-up home folder ----------------------------------------------------------------------

def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def demo_home(home: Path) -> Path:
    mem = home / ".claude/projects/-demo-git-orbit/memory"
    write(home / ".claude/projects/-demo-git-orbit/s.jsonl", '{"cwd": "%s"}\n' % (home / "git" / "orbit"))
    memories = {
        "always-pass-aws-region": ("feedback", "Pass --region explicitly on every aws command",
                                   "Always pass `--region` on every aws CLI command. Dana Whitfield hit 'no region specified' "
                                   "in a CI job where the profile wasn't loaded.\n\n**Why:** the shell default is unreliable.\n\n"
                                   "**How to apply:** add `--region` to every call, including quick checks."),
        "prune-merged-branches": ("feedback", "Delete local branches after their PR merges",
                                  "After a PR merges, delete the local branch and switch back to main.\n\n**Why:** stale "
                                  "branches pile up.\n\n**How to apply:** `git switch main && git branch -d feature`."),
        "run-ci-gates-locally": ("feedback", "Run every CI gate locally before pushing",
                                 "Tests passing isn't CI passing: run fmt, clippy, tests and coverage before every push, and "
                                 "again after any fix.\n\n**Why:** a gate result only holds for the exact tree you push."),
        "release-notes-complete": ("feedback", "Release notes list every change",
                                   "Release notes must list every PR since the last tag, never a highlights summary. Ask "
                                   "arjun.mehta@northwind.example for the template."),
        "user-prefs": ("user", "How the user likes answers", "She prefers short answers and bullet lists."),
        "orbit-status": ("project", "Orbit status", "Orbit's staging environment was torn down on 2026-11-10."),
    }
    for name, (kind, desc, body) in memories.items():
        write(mem / f"{name}.md", f"---\nname: {name}\ndescription: {desc}\ntype: {kind}\n---\n{body}\n")
    repo = home / "git" / "orbit"
    (repo / ".git").mkdir(parents=True)
    write(repo / "CLAUDE.md", "# Orbit\n\n## Testing\n\nRun `pytest -q` before every push; CI runs the same gates.\n\n"
                              "## Diagrams\n\nEvery diagram is Mermaid in the Markdown that uses it; check that it renders.\n")
    vault = home / "Vault"
    (vault / ".obsidian").mkdir(parents=True)
    for person in ("Dana Whitfield", "Arjun Mehta", "Lena Okafor"):
        write(vault / "People" / f"{person}.md", "Colleague.\n")
    write(vault / "Topics/SAML registration.md", "---\ntags: [kungfu, saml]\n---\nTo check whether an SP is registered, "
                                                "query the federation's MDQ service by entityID, not the SP's own metadata. "
                                                "Lena Okafor confirmed this with the federation operator.\n")
    write(vault / "Topics/Vendor call.md", "---\ntags: [kungfu]\n---\nThe vendor is clueless about SAML; we pay $48,000 a year "
                                          "for the license and the renewal is in March.\n")
    write(vault / "Daily/2026-11-20.md", "#kungfu 1:1 notes about Lena's promotion.\n")
    cfg = home / ".config/kungfu/config.toml"
    write(cfg, f'[sources]\nclaude_dir = "{home / ".claude"}"\nrepo_roots = ["{home / "git"}"]\n'
               f'obsidian_vaults = ["{vault}"]\n\n[redact.terms]\n"Northwind" = "the organization"\n')
    return cfg


# --- capture helpers ---------------------------------------------------------------------------

def checklist_frame(fn, keys: list[str]) -> str:
    """Run a checklist-drawing function with scripted keys; return the last frame before it finished."""
    from kungfu import checklist, select

    out = io.StringIO()
    real = checklist.run

    def run(*a, **kw):
        return real(*a, **{**kw, "keys": iter(keys), "out": out, "color": True})

    with mock.patch.object(select.checklist, "run", run):
        try:
            fn()
        except select.Aborted:
            pass
    frames = out.getvalue().split("\x1b[J")
    return frames[-2] if len(frames) > 2 else frames[-1]


def main() -> None:
    os.environ.update(FORCE_COLOR="3", COLORTERM="truecolor", COLUMNS=str(COLS), LINES="40")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        cfg_path = demo_home(home)
        env = {**os.environ, "HOME": str(home), "KUNGFU_CONFIG": str(cfg_path), "KUNGFU_NO_VENV": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null"}
        os.environ.update(HOME=str(home), KUNGFU_CONFIG=str(cfg_path), GIT_CONFIG_GLOBAL="/dev/null")

        from kungfu import config, examine, lessons, select, sensitivity, topics
        from kungfu.extract import Plan, run

        with mock.patch("kungfu.config.auto_redact_terms", return_value={}), \
             mock.patch("kungfu.config.user_identities", return_value={"demo"}):
            # 1. scan
            scan = subprocess.run([sys.executable, str(ROOT / "kungfu"), "scan"], capture_output=True, text=True,
                                  env={**env, "PYTHONPATH": str(ROOT / "src")}).stdout
            svg("$ kungfu scan\n" + scan.split("\nNext:")[0], "kungfu scan", OUT / "scan.svg")

            cfg = config.load()
            result = run(cfg, Plan(vaults=list(cfg.vaults)))

            # 2. init: the People list
            from kungfu.discover import discover

            found = discover(cfg, Plan(vaults=list(cfg.vaults)))
            people = [select.Choice(c.term, c.describe(), c.suggested, value=c) for c in found.candidates.get("person", [])]
            frame = checklist_frame(lambda: select.checklist.run(
                "People: which should be scrubbed from exports?", people, meaning=select.scrub("[person]"),
                note="Unticking one marked \"detected automatically\" means it's never scrubbed."), ["down", "space"])
            svg("$ kungfu init\n…\n" + frame, "kungfu init: choosing what to scrub", OUT / "init-people.svg")

            # 3. export: the topic checklist
            pool = result.offered
            held = {}
            for it in result.held:
                for t in it.topics:
                    held[t] = held.get(t, 0) + 1
            frame = checklist_frame(lambda: select._pick_topics_checklist(topics.counts(pool), "export", held, True, False),
                                    ["space", "down", "space", "down"])
            svg("$ kungfu export\n" + frame, "kungfu export: choosing topics", OUT / "export-topics.svg")

            # 4. export: reviewing the lessons Claude wrote (canned here)
            src = {it.title: it for it in result.items}
            pick = lambda *names: [src[n] for n in names if n in src] or pool[:1]  # noqa: E731
            written = [
                lessons.make("Run every CI gate locally on the exact tree you push",
                             "A passing test suite isn't a passing CI run: run every gate CI runs, and rerun them after any fix.",
                             "A gate result only holds for the exact tree you push; a fix for one gate can break another.",
                             ["Run fmt, lint, tests and coverage locally before pushing.",
                              "After editing to fix a failure, rerun the whole set."],
                             "Repos whose CI gates can run locally.", "Gates that need cloud resources only CI has.",
                             ["ci-cd", "testing"], "high", pick("Run every CI gate locally before pushing", "Testing")),
                lessons.make("Pass --region explicitly on every AWS CLI command",
                             "Don't rely on a profile's default region in agents and CI; pass --region every time.",
                             "Non-interactive shells often don't load the profile that sets the region.",
                             ["Add --region to every aws call, including quick checks."],
                             "Agents, CI and commands handed to someone else.", "An interactive shell you control.",
                             ["aws"], "high", pick("Pass --region explicitly on every aws command")),
                lessons.make("Release notes list every change, verified from diffs",
                             "List every PR since the last tag, checked against its diff, not a summary of highlights.",
                             "Summaries drop operator-facing changes such as renames and new settings.",
                             ["List PRs with git log --first-parent vPREV..main.", "Read each diff, not just its title."],
                             "Releases consumed by operators or integrators.", "Marketing highlights for end customers.",
                             ["ci-cd", "writing"], "medium", pick("Release notes list every change")),
            ]
            for le in written:
                result.redactor.apply(le)
            frame = checklist_frame(lambda: select._review_checklist(written, "Export", result.redactor.apply, True),
                                    ["right"])
            svg("$ kungfu export\n…\n" + frame, "kungfu export: reviewing lessons", OUT / "export-review.svg")

            # 5. import: lessons examined against the recipient's notes (canned verdicts)
            verdicts = [
                ("refines", "Their CI page runs only unit tests locally; the lesson adds lint and coverage, and rerunning "
                            "after fixes.", "", ["wiki/concepts/ci-pipeline.md"]),
                ("conflicts", "Their AWS page relies on each profile's region. That holds in an interactive shell; the "
                              "lesson holds for agents and CI.", "Keep profiles, and add --region in CI jobs and agent "
                              "commands.", ["wiki/tools/aws-cli.md"]),
                ("not_applicable", "Their releases are customer-facing highlights; they have no operators consuming "
                                   "release notes.", "", ["wiki/concepts/release-notes.md"]),
            ]
            received = []
            for le, (v, why, adapt, rel) in zip(written, verdicts):
                copy = lessons.make(le.title, le.meta["statement"], "", [], "", "", le.topics, le.meta["confidence"], [])
                copy.body, copy.meta = le.body, {**le.meta, "verdict": v, "explanation": why, "adaptation": adapt,
                                                 "related": rel}
                received.append(copy)
            frame = checklist_frame(lambda: select._review_checklist(received, "Import", None, False), ["down", "right"])
            svg("$ kungfu import lessons.kungfu --wiki ~/wiki\n…\n" + frame, "kungfu import: lessons examined against your notes",
                OUT / "import-review.svg")

            # 6. the activity indicator
            import random

            from kungfu import rain

            field = rain.Field(COLS, 7, rng=random.Random(7))
            for _ in range(40):
                field.step()
            r = rain.Rain("Examining 12 lesson(s) against your notes", stream=io.StringIO(), force=True)
            r.status("examining lessons 2/3")
            r._start -= 9.4
            svg("\n".join(field.render("truecolor")) + "\n" + r._status_line(), "while it works", OUT / "rain.svg")


if __name__ == "__main__":
    main()
