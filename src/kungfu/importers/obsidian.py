# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Load learnings into an Obsidian vault.

Notes land in ``<vault>/<folder>/<bundle title>/`` (folder defaults to "Kung Fu"), one per
learning, with properties Obsidian understands (``tags`` as ``kungfu/<topic>``, ``aliases``) and a
map-of-content index note linking them by topic. Re-importing skips learnings already present
(matched by ``kungfu_id`` anywhere under the folder) unless ``update`` is set.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from kungfu import examine, frontmatter, risk
from kungfu.bundle import Bundle
from kungfu.importers import Report, existing_ids, neutralize, safe_filename, unique_path, warnings_for, write_text
from kungfu.model import Learning


def _note(it: Learning, bundle: Bundle, today: str) -> str:
    fm = {
        "kungfu_id": it.id,
        "kungfu_bundle": bundle.title,
        "imported": today,
        "trust": "external",
        "kind": it.kind,
        "source_type": it.source_type,
        "tags": [f"kungfu/{t}" for t in it.topics],
        "description": it.summary or None,
        "confidence": it.meta.get("confidence"),
        "evidence": it.meta.get("evidence"),
        "kungfu_verdict": it.meta.get("verdict"),
    }
    origin = bundle.title + (f" by {bundle.manifest['author']}" if bundle.manifest.get("author") else "")
    callout = (
        f"> [!info] Imported learning\n"
        f"> From the kung-fu bundle *{origin}*, imported {today}. "
        f"This is someone else's experience: check it fits your situation before relying on it.\n"
    )
    exam = "\n\n" + examine.section(it, lambda r: f"[[{r[:-3] if r.endswith('.md') else r}]]") if it.meta.get("verdict") else ""
    return frontmatter.dump(fm) + "\n" + callout + "\n" + neutralize(it.body).strip() + exam + "\n"


def _index(bundle: Bundle, placed: list[tuple[Learning, Path]], vault: Path, today: str) -> str:
    fm = {"kungfu_bundle": bundle.title, "imported": today, "tags": ["kungfu"], "trust": "external"}
    lines = [frontmatter.dump(fm), f"# {bundle.title}", ""]
    desc = bundle.manifest.get("description")
    if desc:
        lines += [str(desc), ""]
    noun = "lessons" if bundle.lessons else "learnings"
    lines += [f"Imported {today} with I Know Kung-Fu: {len(placed)} {noun}.", ""]
    by_topic: dict[str, list[tuple[Learning, Path]]] = {}
    for it, p in placed:
        for t in it.topics:
            by_topic.setdefault(t, []).append((it, p))
    for topic in sorted(by_topic):
        lines += [f"## {topic}", ""]
        for it, p in sorted(by_topic[topic], key=lambda x: x[0].title.lower()):
            target = p.relative_to(vault).with_suffix("").as_posix()
            lines.append(f"- [[{target}|{p.stem}]]" + (f" — {it.summary}" if it.summary else ""))
        lines.append("")
    return "\n".join(lines)


def load(bundle: Bundle, items: list[Learning], vault: Path, folder: str = "Kung Fu",
         update: bool = False, dry_run: bool = False, ack: "risk.Acknowledgement | None" = None) -> Report:
    """Write the lessons into vault. Unless this is a dry run, ``ack`` must be the user's
    acknowledgement of the risk for this import (see kungfu.risk); without it nothing is written."""
    if not dry_run:
        risk.require(ack, vault)
    report = Report()
    if not (vault / ".obsidian").is_dir():
        report.notes.append(f"{vault} has no .obsidian folder; writing Markdown there anyway.")
    root = vault / safe_filename(folder)
    dest = root / safe_filename(bundle.title)
    known = existing_ids(root)
    today = date.today().isoformat()
    taken: set[Path] = set()
    placed: list[tuple[Learning, Path]] = []
    for it in items:
        report.warnings += warnings_for(it)
        text = _note(it, bundle, today)
        if it.id in known:
            if update:
                write_text(known[it.id], text, dry_run)
                report.updated.append(known[it.id])
            else:
                report.skipped.append(it.title)
            placed.append((it, known[it.id]))
            continue
        path = unique_path(dest, safe_filename(it.title), taken)
        write_text(path, text, dry_run)
        report.written.append(path)
        placed.append((it, path))
    if report.written or report.updated:
        index = dest / f"{safe_filename(bundle.title)} index.md"
        write_text(index, _index(bundle, placed, vault, today), dry_run)
        report.notes.append(f"Index note: {index.relative_to(vault)}")
    return report
