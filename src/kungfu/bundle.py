# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""The ``.kungfu`` bundle: a zip with a manifest, one Markdown file per learning and a README.

Layout::

    manifest.json
    README.md
    learnings/<id>-<slug>.md     # YAML front matter + body

Only exportable fields are written: no local paths, repo names, flags or the exporter's identity
(an author name is included only when given explicitly). Reading validates every entry before
anything is trusted: path traversal, sizes (zip bombs), checksums and the manifest schema.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from kungfu import __version__, frontmatter
from kungfu.model import KINDS, LESSON_FIELDS, Learning, slugify

FORMAT = "i-know-kung-fu"
FORMAT_VERSION = 1
MAX_ENTRIES = 5000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 50_000_000


class BundleError(Exception):
    pass


class VersionMismatch(BundleError):
    """The bundle was made by a different version of kungfu than the one reading it."""

    def __init__(self, bundle_version: str | None):
        self.bundle_version = bundle_version
        made_by = f"kungfu {bundle_version}" if bundle_version else "an older kungfu that didn't record its version"
        super().__init__(
            f"This bundle was made by {made_by}; this is kungfu {__version__}. Bundles only import into the "
            f"same version of kungfu that exported them, because what a lesson contains and how it was "
            f"scrubbed and checked changes between versions. Ask the sender to export it again with kungfu "
            f"{__version__}" + (f", or import it with kungfu {bundle_version}." if bundle_version
                                else ", or import it with the version of kungfu that made it."))


def bundle_version(manifest: dict) -> str | None:
    """The kungfu version that made a bundle. Bundles from before 0.3.0 only name it in "generator"."""
    v = manifest.get("kungfu_version")
    if v:
        return str(v)
    m = re.match(r"i-know-kung-fu (\S+)$", str(manifest.get("generator", "")))
    return m.group(1) if m else None


@dataclass
class Bundle:
    manifest: dict
    items: list[Learning]

    @property
    def version(self) -> str | None:
        return bundle_version(self.manifest)

    @property
    def lessons(self) -> bool:
        return self.manifest.get("content") == "lessons"

    @property
    def title(self) -> str:
        return self.manifest.get("title") or "Kung-Fu bundle"


def _learning_file(it: Learning) -> str:
    fm = {
        "kungfu_id": it.id,
        "title": it.title,
        "kind": it.kind,
        "topics": it.topics,
        "source_type": it.source_type,
        "summary": it.summary or None,
    }
    fm.update({k: it.meta[k] for k in LESSON_FIELDS if it.meta.get(k)})
    return frontmatter.dump(fm) + "\n" + it.body.strip() + "\n"


def _readme(title: str, description: str, items: list[Learning], topic_counts: dict[str, int]) -> str:
    lines = [f"# {title}", ""]
    if description:
        lines += [description, ""]
    lines += [
        f"A bundle of {len(items)} learnings made with I Know Kung-Fu {__version__}.",
        "",
        "Load it with `kungfu import <this file> --obsidian <vault>` or `--wiki <llm-wiki>`.",
        "",
        "## Topics",
        "",
    ]
    lines += [f"- **{t}** ({n})" for t, n in topic_counts.items()]
    lines += ["", "## Learnings", ""]
    lines += [f"- {it.title} — _{', '.join(it.topics)}_" for it in items]
    return "\n".join(lines) + "\n"


def write(path: Path, items: list[Learning], title: str, description: str = "", author: str | None = None,
          content: str = "details") -> dict:
    """content: "lessons" (synthesized lessons) or "details" (scrubbed learnings as found)."""
    topic_counts: dict[str, int] = {}
    for it in items:
        for t in it.topics:
            topic_counts[t] = topic_counts.get(t, 0) + 1
    topic_counts = dict(sorted(topic_counts.items(), key=lambda kv: (-kv[1], kv[0])))
    entries = []
    files: dict[str, bytes] = {}
    for it in items:
        name = f"learnings/{it.id}-{slugify(it.title, 50)}.md"
        data = _learning_file(it).encode("utf-8")
        files[name] = data
        entries.append({
            "id": it.id,
            "path": name,
            "title": it.title,
            "kind": it.kind,
            "topics": it.topics,
            "source_type": it.source_type,
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "kungfu_version": __version__,
        "generator": f"i-know-kung-fu {__version__}",
        "created": date.today().isoformat(),
        "title": title,
        "description": description,
        "author": author or None,
        "content": content,
        "topics": topic_counts,
        "learnings": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        zf.writestr("README.md", _readme(title, description, items, topic_counts))
        for name, data in files.items():
            zf.writestr(name, data)
    tmp.replace(path)
    return manifest


_SAFE_NAME = re.compile(r"^learnings/[0-9a-f]{16}-[a-z0-9\-]{1,60}\.md$")


def _check_name(name: str) -> None:
    p = PurePosixPath(name)
    if name.startswith("/") or "\\" in name or ".." in p.parts or ":" in name:
        raise BundleError(f"unsafe path in bundle: {name!r}")


def read(path: Path, strict: bool = True) -> Bundle:
    """Read and validate a bundle. With ``strict`` (the default, used by import), a bundle from any
    other version of kungfu is refused with VersionMismatch; ``inspect`` reads with strict=False."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BundleError(f"not a kung-fu bundle: {exc}") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BundleError("bundle has too many files")
        total = 0
        for info in infos:
            _check_name(info.filename)
            if info.file_size > MAX_FILE_BYTES and info.filename != "manifest.json":
                raise BundleError(f"{info.filename} is too large")
            total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise BundleError("bundle is too large when unpacked")
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except (KeyError, ValueError) as exc:
            raise BundleError("bundle has no valid manifest.json") from exc
        if manifest.get("format") != FORMAT:
            raise BundleError("not an I Know Kung-Fu bundle")
        if strict and bundle_version(manifest) != __version__:
            raise VersionMismatch(bundle_version(manifest))
        if int(manifest.get("format_version", 0)) > FORMAT_VERSION:
            raise BundleError("bundle was made by a newer version of kungfu; upgrade to read it")
        items = []
        for entry in manifest.get("learnings", []):
            name = str(entry.get("path", ""))
            if not _SAFE_NAME.match(name):
                raise BundleError(f"unexpected learning path {name!r}")
            data = zf.read(name)
            if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                raise BundleError(f"checksum mismatch for {name}")
            fm, body = frontmatter.split(data.decode("utf-8", errors="replace"))
            kind = str(fm.get("kind") or "note")
            item = Learning(
                title=str(fm.get("title") or entry.get("title") or "Untitled")[:200],
                body=body.strip(),
                source_type=str(fm.get("source_type") or "unknown")[:40],
                kind=kind if kind in KINDS else "note",
                summary=str(fm.get("summary") or "")[:500],
                topics=[slugify(str(t), 40) for t in (fm.get("topics") or ["general"])][:10],
                meta={k: str(fm[k])[:2000] for k in LESSON_FIELDS if fm.get(k)},
            )
            # The id is recomputed from content; a mismatch means the file was altered after packing.
            if item.id != fm.get("kungfu_id"):
                item.flag("id-mismatch")
            items.append(item)
    return Bundle(manifest=manifest, items=items)
