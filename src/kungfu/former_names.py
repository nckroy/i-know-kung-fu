# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Former names (a deadname, a previous legal name, old usernames and addresses).

They must never appear in an export, and they must not sit in plain text on disk either. They're
kept in their own file next to the config (``former-names.json``, mode 0600), never in
``config.toml``, and only as salted HMAC-SHA256 hashes. Text is checked by hashing every word and
short run of words and comparing.

What the hashing protects against: someone reading, grepping, syncing, backing up or being shown
the file sees no names. What it doesn't: a targeted guess. Anyone with the file and a list of
candidate names can test them, as with any hash of a short string. Hashing every n-gram of the
text needs a fast hash, so this is obscurity of a short secret, not encryption.

Each entry also covers its parts and common usernames built from it: "Jordan Example" covers
"Jordan", "Example", "jexample", "jordan.example" and so on. A match is replaced with "the user",
exactly like your current name, so nothing in an export hints that a former name existed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import unicodedata
from pathlib import Path

VERSION = 1
TOKEN = re.compile(r"[^\W_][\w'’.@+\-]*", re.UNICODE)
PART = re.compile(r"[^\W_]+", re.UNICODE)
POSSESSIVE = re.compile(r"(?:'s|’s|'|’)$", re.I)


def default_path() -> Path:
    env = os.environ.get("KUNGFU_FORMER_NAMES")
    if env:
        return Path(env).expanduser()
    from kungfu.config import config_path

    return config_path().parent / "former-names.json"


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(".,;:!?\"'()[]{}<>`’“”")


def variants(raw: str) -> tuple[set[str], set[str]]:
    """Forms an entry should match: (always, only-when-capitalized).

    Always: the whole entry, an email's local part, and usernames derived from a full name.
    The separate words of a name match too, but a word that is also ordinary English ("Will",
    "Grace", "Rose") matches only when capitalized in the text, so "will" stays "will".
    """
    from kungfu.config import dictionary_words, name_variants

    raw = raw.strip()
    always, capitalized = {raw}, set()
    words_dict = dictionary_words()
    parts: set[str] = set()
    if "@" in raw:
        local = raw.split("@", 1)[0]
        always.add(local)
        parts |= {p for p in PART.findall(local) if len(p) >= 3}
    words = raw.split()
    if len(words) > 1:
        parts |= {w for w in words if len(normalize(w)) >= 3}
        always |= name_variants(raw)
    for p in parts:
        (capitalized if normalize(p) in words_dict else always).add(p)
    norm = lambda xs: {normalize(x) for x in xs if normalize(x)}  # noqa: E731
    always_n = norm(always)
    return always_n, norm(capitalized) - always_n


class FormerNames:
    def __init__(self, salt: bytes | None = None, hashes: set[str] | None = None, max_words: int = 1,
                 entries: int = 0, path: Path | None = None, cap_hashes: set[str] | None = None) -> None:
        self.salt = salt or secrets.token_bytes(16)
        self.hashes = hashes or set()
        self.cap_hashes = cap_hashes or set()  # match only when the word is capitalized in the text
        self.max_words = max_words
        self.entries = entries  # how many things the person typed (shown as a count, never listed)
        self.path = path

    # --- storage -----------------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "FormerNames":
        path = path or default_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(bytes.fromhex(data["salt"]), set(data.get("hashes", [])), int(data.get("max_words", 1)),
                       int(data.get("entries", 0)), path, set(data.get("cap_hashes", [])))
        except (OSError, ValueError, KeyError):
            return cls(path=path)

    def save(self, path: Path | None = None) -> Path:
        path = path or self.path or default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": VERSION, "salt": self.salt.hex(), "hashes": sorted(self.hashes),
                       "cap_hashes": sorted(self.cap_hashes),
                       "max_words": self.max_words, "entries": self.entries}, fh)
        tmp.replace(path)
        os.chmod(path, 0o600)
        self.path = path
        return path

    def clear(self) -> None:
        self.hashes.clear()
        self.cap_hashes.clear()
        self.entries = 0
        self.max_words = 1
        self.salt = secrets.token_bytes(16)

    # --- matching ----------------------------------------------------------------------------

    def _h(self, s: str) -> str:
        return hmac.new(self.salt, normalize(s).encode(), hashlib.sha256).hexdigest()[:32]

    def add(self, raw: str) -> int:
        """Add one name, username or address (and its forms). Returns how many forms were added."""
        always, capitalized = variants(raw)
        before = len(self.hashes) + len(self.cap_hashes)
        for f in always:
            self.hashes.add(self._h(f))
            self.max_words = max(self.max_words, len(f.split()))
        for f in capitalized:
            self.cap_hashes.add(self._h(f))
        if always:
            self.entries += 1
        return len(self.hashes) + len(self.cap_hashes) - before

    def __bool__(self) -> bool:
        return bool(self.hashes)

    def matches(self, s: str) -> bool:
        if not self.hashes or not normalize(s):
            return False
        h = self._h(s)
        return h in self.hashes or (s[:1].isupper() and h in self.cap_hashes)

    def find(self, text: str) -> list[tuple[int, int]]:
        """Spans of text that are a former name (or part of a username/address built from one)."""
        if not self.hashes:
            return []
        toks = []
        for m in TOKEN.finditer(text):
            core = m.group(0).rstrip(".")
            core_trim = POSSESSIVE.sub("", core)
            toks.append((m.start(), m.start() + len(core_trim), core_trim))
        spans: list[tuple[int, int]] = []
        i = 0
        while i < len(toks):
            hit = None
            for n in range(min(self.max_words, len(toks) - i), 0, -1):
                group = toks[i:i + n]
                if any(text[a[1]:b[0]].strip(" \t") for a, b in zip(group, group[1:])):
                    continue  # words must be separated only by spaces
                if self.matches(" ".join(t[2] for t in group)):
                    hit = (group[0][0], group[-1][1], n)
                    break
            if hit:
                spans.append(hit[:2])
                i += hit[2]
                continue
            start, _, word = toks[i]
            pieces = [word.split("@", 1)[0]] if "@" in word else []
            for piece in pieces:
                if self.matches(piece):
                    spans.append((start, start + len(piece)))
                    break
            else:
                for p in PART.finditer(word):
                    if len(p.group(0)) >= 3 and self.matches(p.group(0)):
                        spans.append((start + p.start(), start + p.end()))
            i += 1
        return spans

    def scrub(self, text: str, replacement: str = "the user") -> tuple[str, int]:
        spans = self.find(text)
        for a, b in sorted(spans, reverse=True):
            text = text[:a] + replacement + text[b:]
        return text, len(spans)


_CACHE: dict[str, FormerNames] = {}


def current() -> FormerNames:
    """The saved former names for this config (cached per path)."""
    path = default_path()
    key = str(path)
    if key not in _CACHE:
        _CACHE[key] = FormerNames.load(path)
    return _CACHE[key]


def forget_cache() -> None:
    _CACHE.clear()
