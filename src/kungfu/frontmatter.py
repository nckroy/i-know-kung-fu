# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Just enough YAML front matter handling for Markdown notes, using only the standard library.

Reading supports the subset that Obsidian, Claude Code memories and LLM wikis write:
scalars, quoted strings, inline lists ``[a, b]``, block lists and nested mappings.
Writing always emits JSON-quoted scalars, which are valid YAML and round-trip safely.
"""

from __future__ import annotations

import json
import re

_FENCE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def split(text: str) -> tuple[dict, str]:
    """Return (front matter dict, body). Unparseable front matter yields an empty dict."""
    m = _FENCE.match(text)
    if not m:
        return {}, text
    try:
        data = _parse_block(m.group(1).splitlines())
    except Exception:
        data = {}
    return (data if isinstance(data, dict) else {}), text[m.end():]


def _scalar(raw: str):
    s = raw.strip()
    if not s:
        return None
    if s[0] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s.strip('"')
    if s[0] == "'" and s.endswith("'") and len(s) >= 2:
        return s[1:-1].replace("''", "'")
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _split_inline(inner)]
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    # Strip trailing comments on unquoted scalars.
    s = re.sub(r"\s+#.*$", "", s)
    return s


def _split_inline(inner: str) -> list[str]:
    parts, buf, quote = [], [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in (x.strip() for x in parts) if p]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_block(lines: list[str]):
    lines = [ln.rstrip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    value, _ = _parse_node(lines, 0, _indent(lines[0]) if lines else 0)
    return value


def _parse_node(lines: list[str], i: int, indent: int):
    if i >= len(lines):
        return None, i
    if lines[i].lstrip().startswith("- ") or lines[i].strip() == "-":
        out = []
        while i < len(lines) and _indent(lines[i]) == indent and lines[i].lstrip().startswith("-"):
            item = lines[i].lstrip()[1:].strip()
            i += 1
            if item:
                out.append(_scalar(item))
            elif i < len(lines) and _indent(lines[i]) > indent:
                val, i = _parse_node(lines, i, _indent(lines[i]))
                out.append(val)
            else:
                out.append(None)
        return out, i
    out: dict = {}
    while i < len(lines) and _indent(lines[i]) == indent:
        line = lines[i].strip()
        if ":" not in line:
            i += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip().strip("\"'")
        i += 1
        if rest.strip():
            out[key] = _scalar(rest)
        elif i < len(lines) and (_indent(lines[i]) > indent or lines[i].lstrip().startswith("- ")):
            child_indent = _indent(lines[i])
            out[key], i = _parse_node(lines, i, child_indent)
        else:
            out[key] = None
    return out, i


def dump(data: dict) -> str:
    """Serialize a dict as a YAML front matter block (including the --- fences)."""
    lines = ["---"]
    _emit(data, lines, 0)
    lines.append("---")
    return "\n".join(lines) + "\n"


def _emit(data: dict, lines: list[str], level: int) -> None:
    pad = "  " * level
    for key, val in data.items():
        if val is None:
            continue
        if isinstance(val, bool):
            lines.append(f"{pad}{key}: {'true' if val else 'false'}")
        elif isinstance(val, (int, float)):
            lines.append(f"{pad}{key}: {val}")
        elif isinstance(val, dict):
            lines.append(f"{pad}{key}:")
            _emit(val, lines, level + 1)
        elif isinstance(val, (list, tuple)):
            if not val:
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}:")
                lines.extend(f"{pad}  - {json.dumps(str(v), ensure_ascii=False)}" for v in val)
        else:
            lines.append(f"{pad}{key}: {json.dumps(str(val), ensure_ascii=False)}")
