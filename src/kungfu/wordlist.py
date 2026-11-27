# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Words that hold a learning back (profanity, slurs), stored only as hashes.

The words themselves are never written down in this repository. ``data/blocked_words.txt`` holds a
keyed BLAKE2b hash of each one, and text is checked by hashing its words and looking them up. That
keeps the list out of reach of grep, code review, search indexes and anyone reading the source,
unlike an encoding such as ROT13, which anyone can undo at a glance.

Each entry is ``<category> <kind> <hash>``:

  category  ``profanity`` or ``slur``
  kind      ``exact``: matches the whole word, or a two-word phrase separated by one space
            ``prefix``: matches any word that starts with it, so one entry covers "-ing", "-ed", "-s"…

Words are compared after NFKC normalization and case folding, with curly apostrophes made straight.
A word is a run of letters, digits and underscores, as in a regex ``\\w+``; a word joined by an
apostrophe ("don't") is checked whole as well as in parts. Censored spellings, with letters replaced
by ``*``, ``@`` or ``#``, are checked too: any run of those symbols counts as a single ``*``, so an
entry written with one ``*`` matches them all.

Tests never use the real words: they run against a made-up list. To check the real list against
real words, keep a file outside the repo and point ``KUNGFU_BLOCKED_WORDS_FIXTURE`` at it; see
``tests/test_kungfu.py`` for its format.

To add words without putting them in your shell history, pipe them in one per line:

  python3 -m kungfu.wordlist add profanity --prefix < words.txt
  python3 -m kungfu.wordlist check < some-text.md
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).parent / "data" / "blocked_words.txt"
CATEGORIES = ("profanity", "slur")
KINDS = ("exact", "prefix")
# A fixed key, so these hashes don't match generic unkeyed hash lists of the same words.
_KEY = b"i-know-kung-fu/blocked-words/v1"
_MIN_PREFIX, _MAX_PREFIX = 3, 32

_WORD = re.compile(r"\w+")
_APOSTROPHE_WORD = re.compile(r"\w+(?:'\w+)+")
_PAIR = re.compile(r"(?=\b(\w+) (\w+)\b)")
_CENSORED = re.compile(r"(?<!\w)\w+(?:[*@#]+\w*)+")
_SYMBOLS = re.compile(r"[*@#]+")


def normalize(word: str) -> str:
    word = unicodedata.normalize("NFKC", word).casefold().replace("’", "'").strip()
    return _SYMBOLS.sub("*", word)


def digest(word: str) -> str:
    return hashlib.blake2b(normalize(word).encode("utf-8"), key=_KEY, digest_size=16).hexdigest()


@lru_cache(maxsize=1)
def _table() -> dict[str, dict[str, str]]:
    """{kind: {hash: category}}; empty when the list is missing."""
    table: dict[str, dict[str, str]] = {k: {} for k in KINDS}
    try:
        lines = PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return table
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and not line.startswith("#") and parts[0] in CATEGORIES and parts[1] in KINDS:
            table[parts[1]][parts[2]] = parts[0]
    return table


@lru_cache(maxsize=65536)
def _word_hits(word: str) -> frozenset[str]:
    table = _table()
    hits = set()
    if (c := table["exact"].get(digest(word))):
        hits.add(c)
    if table["prefix"]:
        for n in range(_MIN_PREFIX, min(len(word), _MAX_PREFIX) + 1):
            if (c := table["prefix"].get(digest(word[:n]))):
                hits.add(c)
    return frozenset(hits)


def categories(text: str) -> set[str]:
    """The categories ("profanity", "slur") of the blocked words in the text."""
    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    found: set[str] = set()
    words = set(_WORD.findall(text)) | set(_APOSTROPHE_WORD.findall(text))
    words |= {_SYMBOLS.sub("*", w.rstrip("*@#")) for w in _CENSORED.findall(text)}
    for word in words:
        found |= _word_hits(word)
    exact = _table()["exact"]
    for a, b in _PAIR.findall(text):
        if (c := exact.get(digest(f"{a} {b}"))):
            found.add(c)
    return found


def add(category: str, kind: str, words: list[str]) -> int:
    """Append hashes for the words to the list; returns how many were new."""
    if category not in CATEGORIES or kind not in KINDS:
        raise ValueError(f"unknown category or kind: {category} {kind}")
    have = {line.split()[2] for line in PATH.read_text(encoding="utf-8").splitlines()
            if len(line.split()) == 3 and not line.startswith("#")} if PATH.exists() else set()
    new = []
    for w in words:
        w = normalize(w)
        if not w:
            continue
        if kind == "prefix" and not (_MIN_PREFIX <= len(w) <= _MAX_PREFIX and re.fullmatch(r"\w[\w*]*", w)):
            raise ValueError(f"a prefix must be one word of {_MIN_PREFIX}-{_MAX_PREFIX} characters")
        if kind == "exact" and not re.fullmatch(r"\w[\w*]*(?:'\w+)*(?: \w+)?", w):
            raise ValueError("an exact entry must be one word, or two words separated by a space")
        h = digest(w)
        if h not in have:
            have.add(h)
            new.append(f"{category} {kind} {h}\n")
    if new:
        with PATH.open("a", encoding="utf-8") as f:
            f.writelines(new)
        _table.cache_clear()
        _word_hits.cache_clear()
    return len(new)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m kungfu.wordlist", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="hash words read from stdin, one per line, into the list")
    a.add_argument("category", choices=CATEGORIES)
    a.add_argument("--prefix", action="store_true", help="match words that start with these")
    sub.add_parser("check", help="print the categories of blocked words found in text read from stdin")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        n = add(args.category, "prefix" if args.prefix else "exact", sys.stdin.read().splitlines())
        print(f"added {n} new {'entry' if n == 1 else 'entries'} to {PATH}")
    else:
        print(" ".join(sorted(categories(sys.stdin.read()))) or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
