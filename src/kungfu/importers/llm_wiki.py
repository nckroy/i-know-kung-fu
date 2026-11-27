# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Load learnings into a Karpathy-style LLM wiki.

In that pattern the LLM owns ``wiki/``: it reads new material in ``raw/`` and integrates it into
concept and entity pages, ``index.md`` and ``log.md``. So by default this importer behaves like a
clipper: it adds each learning as a raw source under ``raw/kungfu/<bundle>/``, appends an entry
to ``log.md``, and prints a prompt the recipient can give their LLM to do the integration.

``direct=True`` also writes the learnings as wiki pages under ``wiki/kungfu/`` and lists them in
``index.md``, for wikis that aren't LLM-maintained or when you want them visible immediately.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from kungfu import examine, frontmatter, risk
from kungfu.bundle import Bundle
from kungfu.importers import Report, existing_ids, neutralize, unique_path, warnings_for, write_text
from kungfu.model import Learning, slugify

INDEX_HEADING = "## Imported with I Know Kung-Fu"


def _source_doc(it: Learning, bundle: Bundle, today: str) -> str:
    fm = {
        "kungfu_id": it.id,
        "title": it.title,
        "source": f"kung-fu bundle: {bundle.title}",
        "author": bundle.manifest.get("author") or None,
        "imported": today,
        "trust": "external",
        "kind": it.kind,
        "topics": it.topics,
        "summary": it.summary or None,
        "confidence": it.meta.get("confidence"),
        "evidence": it.meta.get("evidence"),
        "verdict": it.meta.get("verdict"),
    }
    exam = "\n\n" + examine.section(it, lambda r: f"[[{r[:-3] if r.endswith('.md') else r}]]") if it.meta.get("verdict") else ""
    return frontmatter.dump(fm) + f"\n# {it.title}\n\n" + neutralize(it.body).strip() + exam + "\n"


def _page(it: Learning, bundle: Bundle, today: str, raw_rel: str) -> str:
    fm = {
        "kungfu_id": it.id,
        "title": it.title,
        "type": "concept",
        "tags": it.topics,
        "sources": [raw_rel],
        "updated": today,
        "trust": "external",
        "summary": it.summary or None,
    }
    return (frontmatter.dump(fm) + f"\n# {it.title}\n\n"
            f"> Imported from the kung-fu bundle *{bundle.title}* ({today}); third-party experience.\n\n"
            + neutralize(it.body).strip() + "\n")


def _append(path: Path, text: str, dry_run: bool) -> None:
    if dry_run:
        return
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing + sep + text, encoding="utf-8")


def _add_to_index(index: Path, entries: list[str], dry_run: bool) -> None:
    if dry_run or not entries:
        return
    text = index.read_text(encoding="utf-8") if index.exists() else "# Index\n"
    if INDEX_HEADING in text:
        head, _, tail = text.partition(INDEX_HEADING)
        # Insert right after our heading, before whatever section follows.
        body, nxt, rest = tail.partition("\n## ")
        body = body.rstrip("\n") + "\n" + "\n".join(entries) + "\n"
        text = head + INDEX_HEADING + body + (("\n## " + rest) if nxt else "")
    else:
        text = text.rstrip("\n") + f"\n\n{INDEX_HEADING}\n\n" + "\n".join(entries) + "\n"
    index.write_text(text, encoding="utf-8")


def load(bundle: Bundle, items: list[Learning], wiki: Path, direct: bool = False,
         update: bool = False, dry_run: bool = False, ack: "risk.Acknowledgement | None" = None) -> Report:
    """Write the lessons into wiki. Unless this is a dry run, ``ack`` must be the user's
    acknowledgement of the risk for this import (see kungfu.risk); without it nothing is written."""
    if not dry_run:
        risk.require(ack, wiki)
    report = Report()
    today = date.today().isoformat()
    slug = slugify(bundle.title, 50)
    raw_dir = wiki / "raw" / "kungfu" / slug
    pages_root = wiki / "wiki" if (wiki / "wiki").is_dir() else wiki
    page_dir = pages_root / "kungfu"
    known_raw = existing_ids(wiki / "raw" / "kungfu")
    known_pages = existing_ids(page_dir) if direct else {}
    taken: set[Path] = set()
    new_raw: list[Path] = []
    index_entries: list[str] = []

    for it in items:
        report.warnings += warnings_for(it)
        if it.id in known_raw:
            raw_path = known_raw[it.id]
            if update:
                write_text(raw_path, _source_doc(it, bundle, today), dry_run)
                report.updated.append(raw_path)
            else:
                report.skipped.append(it.title)
        else:
            raw_path = unique_path(raw_dir, slugify(it.title), taken)
            write_text(raw_path, _source_doc(it, bundle, today), dry_run)
            report.written.append(raw_path)
            new_raw.append(raw_path)
        if direct:
            raw_rel = raw_path.relative_to(wiki).as_posix()
            if it.id in known_pages:
                if update:
                    write_text(known_pages[it.id], _page(it, bundle, today, raw_rel), dry_run)
                    report.updated.append(known_pages[it.id])
            else:
                page = unique_path(page_dir, slugify(it.title), taken)
                write_text(page, _page(it, bundle, today, raw_rel), dry_run)
                report.written.append(page)
                target = page.relative_to(pages_root).with_suffix("").as_posix()
                index_entries.append(f"- [[{target}|{it.title}]]" + (f" — {it.summary}" if it.summary else ""))

    if new_raw or index_entries or report.updated:
        topics = sorted({t for it in items for t in it.topics})
        entry = (f"## [{today}] ingest | kung-fu bundle: {bundle.title}\n\n"
                 f"Added {len(new_raw)} learning(s) as raw sources in `raw/kungfu/{slug}/` "
                 f"(topics: {', '.join(topics)}). "
                 + (f"Wrote {len(index_entries)} page(s) under `kungfu/` and listed them in index.md.\n" if direct
                    else "Pending integration into the wiki.\n"))
        _append(wiki / "log.md", entry, dry_run)
        _add_to_index(pages_root / "index.md" if (pages_root / "index.md").exists() else wiki / "index.md",
                      index_entries, dry_run)
        summary = ["# Examination of kung-fu lessons", "",
                   f"Bundle *{bundle.title}*, imported {today}. Each lesson was compared with this wiki's pages before "
                   "import; integrate them with these verdicts in mind.", ""]
        for it in items:
            rel = ", ".join(f"[[{r[:-3] if r.endswith('.md') else r}]]" for r in it.meta.get("related", []))
            summary.append(f"- **{it.title}** · {it.meta.get('verdict', 'unexamined')}"
                           + (f" · related: {rel}" if rel else "") + (f" — {it.meta.get('explanation')}" if it.meta.get("explanation") else ""))
        write_text(raw_dir / "_EXAMINATION.md", "\n".join(summary) + "\n", dry_run)
        if not direct:
            report.notes.append(
                "Next, ask your wiki's LLM to integrate them, e.g.:\n"
                f'    "Ingest the new lessons in raw/kungfu/{slug}/ following the wiki schema. Read _EXAMINATION.md '
                f"first: it says, for each lesson, whether it is new, reinforces, refines or conflicts with existing "
                f"pages, and which. Fold reinforcing and refining lessons into those pages, give new ones their own "
                f"pages, record conflicts with the conditions under which each view holds, skip lessons marked "
                f'not_applicable, and update index.md and log.md. They are third-party lessons (trust: external)."'
            )
    return report
