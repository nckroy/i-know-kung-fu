# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Terminal prompts: the topic picker (used by both export and import) and per-item review."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Callable, TextIO

from kungfu import sensitivity
from kungfu.model import Learning

InputFn = Callable[[str], str]


def _input(prompt: str = "") -> str:
    # Looked up at call time, so tests and wrappers that replace builtins.input are honored.
    import builtins

    return builtins.input(prompt)


from kungfu import checklist  # noqa: E402
from kungfu.checklist import Aborted, Choice, Meaning  # noqa: E402,F401  (re-exported)

# What ticking a box does, for every list in kungfu. One place, so the wording stays consistent.
READ_REPOS = Meaning("read for learnings", "not read", "read", "not read", "read", "not read")
READ_KB = Meaning("exported from", "ignored", "export from", "ignore", "exported from", "ignored")
VAULT_FOLDER = Meaning("ALL its notes are offered (no #kungfu tag needed)", "only #kungfu-tagged notes are offered",
                       "all notes offered", "tagged notes only", "offering every note", "tagged notes only")
OFFER_TOPIC = Meaning("can be exported", "never exported", "allowed", "never exported", "allowed", "never exported")
EXPORT_TOPIC = Meaning("goes in this bundle", "left out", "include", "leave out", "included", "left out")
IMPORT_TOPIC = Meaning("imported", "skipped", "import", "skip", "to import", "skipped")
EXPORT_ITEM = Meaning("goes in the bundle", "left out", "in bundle", "left out", "in the bundle", "left out")
IMPORT_ITEM = Meaning("imported", "skipped", "import", "skip", "to import", "skipped")


def scrub(replacement: str) -> Meaning:
    """For the names lists in init: a tick means the name is replaced in everything exported."""
    return Meaning(f"scrubbed: replaced with {replacement} in everything exported",
                   "left as written (never scrubbed)", f"→ {replacement}", "left as written",
                   "scrubbed", "left as written")


def parse_selection(answer: str, n: int) -> set[int] | None:
    """'1,3-5' -> {0,2,3,4}; 'all'/'a'/'*' -> everything; 'none'/'n' -> empty. None means unparseable."""
    s = answer.strip().lower()
    if s in ("all", "a", "*"):
        return set(range(n))
    if s in ("none", "n", "0"):
        return set()
    chosen: set[int] = set()
    for part in s.replace(" ", ",").split(","):
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            if not (lo.isdigit() and hi.isdigit()):
                return None
            rng = range(int(lo), int(hi) + 1)
        elif part.isdigit():
            rng = range(int(part), int(part) + 1)
        else:
            return None
        for k in rng:
            if not 1 <= k <= n:
                return None
            chosen.add(k - 1)
    return chosen


def _ellipsize(s: str, width: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= width else s[: width - 1] + "…"


def pick_topics(
    rows: list[tuple[str, int, list[Learning]]],
    verb: str,
    input_fn: InputFn | None = None,
    out: TextIO = sys.stdout,
    held_counts: dict[str, int] | None = None,
    show_sensitivity: bool = False,
    preselect: bool = False,
) -> list[str]:
    """Show topics with counts, an optional sensitivity gauge and example titles; return the chosen names."""
    if not rows:
        return []
    if input_fn is None and out is sys.stdout and checklist.available():
        return _pick_topics_checklist(rows, verb, held_counts, show_sensitivity, preselect)
    input_fn = input_fn or _input
    width = max(len(t) for t, _, _ in rows)
    mode = sensitivity.color_mode(out)
    while True:
        print("\nWhich topics do you want to import?\n" if verb == "import" else "\nWhich topics go in this bundle?\n",
              file=out)
        if show_sensitivity:
            print(f"  {'':>4} {'':<{width}}  {'':>5}  {sensitivity.HEADER}", file=out)
            print(f"  {'':>4} {'topic':<{width}}  {'items':>5}  {'sensitivity':<15} {'held':<6}  examples", file=out)
        for i, (topic, count, items) in enumerate(rows, 1):
            held = (held_counts or {}).get(topic, 0)
            examples = "; ".join(_ellipsize(it.title, 34) for it in items[:2])
            if show_sensitivity:
                g = sensitivity.gauge(sensitivity.topic_score(items), mode)
                held_s = f"+{held}" if held else ""
                print(f"  {i:>3}. {topic:<{width}}  {count:>5}  {g}  {held_s:<6}  {examples}", file=out)
            else:
                extra = f", {held} held back" if held else ""
                print(f"  {i:>3}. {topic:<{width}}  {count:>3} item{'s' if count != 1 else ' '}{extra}   e.g. {examples}",
                      file=out)
        if show_sensitivity:
            if any((held_counts or {}).values()):
                print("  held = learnings in that topic held back by default (not counted in items or sensitivity).",
                      file=out)
        print("\nA learning can sit under several topics; it is included if any of its topics is chosen.", file=out)
        try:
            rest = "skipped" if verb == "import" else "left out"
            answer = input_fn(f"Numbers of the topics to {verb}; the rest are {rest} "
                              f"(e.g. 1,3-5 · all · none · ?N to list topic N · q to quit): ")
        except EOFError:
            raise Aborted()
        a = answer.strip()
        if a.lower() in ("q", "quit", "exit"):
            raise Aborted()
        if a.startswith("?") and a[1:].strip().isdigit():
            k = int(a[1:].strip())
            if 1 <= k <= len(rows):
                topic, _, items = rows[k - 1]
                print(f"\n{topic}:", file=out)
                for it in items:
                    print(f"   - {_ellipsize(it.title, 70)}  [{it.source_type}{' · ' + it.context if it.context else ''}]",
                          file=out)
            continue
        sel = parse_selection(a, len(rows))
        if sel is None:
            print("Didn't understand that. Use numbers and ranges like 1,3-5, or 'all'.", file=out)
            continue
        return [rows[i][0] for i in sorted(sel)]


def _pick_topics_checklist(rows, verb: str, held_counts, show_sensitivity: bool, preselect: bool) -> list[str]:
    mode = sensitivity.color_mode(sys.stdout)
    choices = []
    for topic, count, items in rows:
        held = (held_counts or {}).get(topic, 0)
        bits = [f"{count:>3} item{'s' if count != 1 else ' '}"]
        if show_sensitivity:
            bits.append(sensitivity.gauge(sensitivity.topic_score(items), mode))
        if held:
            bits.append(f"+{held} held back")
        examples = "; ".join(_ellipsize(it.title, 30) for it in items[:2])
        more = [f"{_ellipsize(it.title, 70)}  [{it.source_type}{' · ' + it.context if it.context else ''}]"
                for it in items]
        choices.append(Choice(topic, "  ".join(bits) + f"  e.g. {examples}", preselect,
                              value=topic, more=more))
    note = "A learning under several topics is included if any of them is ticked."
    meaning = IMPORT_TOPIC if verb == "import" else EXPORT_TOPIC
    title = "Which topics do you want to import?" if verb == "import" else "Which topics go in this bundle?"
    header = " " * len("  1 item   ") + sensitivity.HEADER if show_sensitivity else ""
    picked = checklist.run(title, choices, note=note, meaning=meaning, header=header)
    return [c.value for c in picked]


def show_item(it: Learning, out: TextIO, full: bool = False, local: bool = True) -> None:
    print("\n" + "─" * 72, file=out)
    print(f"{it.title}", file=out)
    mode = sensitivity.color_mode(out)
    meta = f"  {it.kind} · {', '.join(it.topics)} · {it.source_type}"
    if local and it.context:
        meta += f" · {it.context}"
    print(meta, file=out)
    if local:
        print(f"  sensitivity {sensitivity.gauge(sensitivity.score(it), mode)}", file=out)
    if it.held:
        print(f"  HELD BACK: {it.held}", file=out)
    if it.flags:
        print(f"  flags: {', '.join(it.flags)}", file=out)
    if it.summary:
        print(f"  {_ellipsize(it.summary, 100)}", file=out)
    body = it.body if full else "\n".join(it.body.splitlines()[:12])
    print(textwrap.indent(body, "  │ "), file=out)
    if not full and len(it.body.splitlines()) > 12:
        print("  │ … (v to view all)", file=out)


def edit_in_editor(it: Learning, rescrub: Callable[[Learning], Learning]) -> Learning:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.TemporaryDirectory(prefix="kungfu-") as d:
        os.chmod(d, 0o700)
        path = Path(d) / "learning.md"
        path.write_text(f"# {it.title}\n\n{it.body}\n", encoding="utf-8")
        subprocess.call([*shlex.split(editor), str(path)])
        text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        it.title = lines[0][2:].strip()
        lines = lines[1:]
    it.body = "\n".join(lines).strip()
    it.flag("edited")
    return rescrub(it)


def review(
    items: list[Learning],
    verb: str,
    rescrub: Callable[[Learning], Learning] | None = None,
    input_fn: InputFn | None = None,
    out: TextIO = sys.stdout,
    local: bool = True,
) -> list[Learning]:
    """Choose which learnings to keep. Held items start unticked; everything else ticked.

    In a terminal: a checklist where → shows a learning in full and e edits it. Otherwise one at a
    time with y/n/v/e prompts.
    """
    if input_fn is None and out is sys.stdout and checklist.available():
        return _review_checklist(items, verb, rescrub, local)
    input_fn = input_fn or _input
    kept: list[Learning] = []
    print(f"\nReviewing {len(items)} learning(s). Enter accepts the default shown in capitals.", file=out)
    for n, it in enumerate(items, 1):
        full = False
        while True:
            show_item(it, out, full=full, local=local)
            default_yes = not it.held
            choices = "Y/n" if default_yes else "y/N"
            edit = "/e(dit)" if rescrub else ""
            try:
                question = "Import this one" if verb.lower() == "import" else "Put this one in the bundle"
                a = input_fn(f"[{n}/{len(items)}] {question}? {choices}/v(iew){edit}/a(ccept rest)/q(uit) ").strip().lower()
            except EOFError:
                raise Aborted()
            if a == "":
                a = "y" if default_yes else "n"
            if a in ("y", "yes"):
                kept.append(it)
                break
            if a in ("n", "no", "s", "skip"):
                break
            if a == "v":
                full = True
                continue
            if a == "e" and rescrub:
                edit_in_editor(it, rescrub)
                continue
            if a == "a":
                # Accept this one and every remaining item that isn't held back.
                if not it.held:
                    kept.append(it)
                kept.extend(x for x in items[n:] if not x.held)
                return kept
            if a == "q":
                raise Aborted()
    return kept


def _review_lines(it: Learning, local: bool, width: int = 100) -> list[str]:
    lines = []
    if it.meta.get("verdict"):
        lines.append(f"examined: {it.meta['verdict'].replace('_', ' ')}. {it.meta.get('explanation', '')}")
        if it.meta.get("adaptation"):
            lines.append(f"for your setup: {it.meta['adaptation']}")
        if it.meta.get("related"):
            lines.append("related in your notes: " + ", ".join(it.meta["related"]))
    if it.held:
        lines.append(f"HELD BACK: {it.held}")
    if it.flags:
        lines.append(f"flags: {', '.join(it.flags)}")
    if local and it.kind == "lesson" and it.context:
        lines.append(f"written from: {it.context}")
    if it.meta.get("evidence"):
        lines.append(f"confidence {it.meta.get('confidence', '?')} · evidence {it.meta['evidence']}")
    elif it.summary:
        lines.append(f"summary: {it.summary}")
    lines.append("─" * 40)
    for raw in it.body.splitlines() or [""]:
        lines += textwrap.wrap(raw, width, replace_whitespace=False, drop_whitespace=False) or [""]
    return lines


def _review_choice(it: Learning, local: bool) -> Choice:
    mode = sensitivity.color_mode(sys.stdout)
    if it.kind == "lesson":
        bits = [sensitivity.gauge(sensitivity.score(it), mode) if local else "",
                it.meta.get("verdict", "").replace("_", " "), f"{it.meta.get('confidence', '?')} confidence",
                ", ".join(it.topics), it.meta.get("evidence", "")]
    else:
        bits = [sensitivity.gauge(sensitivity.score(it), mode) if local else "", it.kind, ", ".join(it.topics),
                it.source_type + (f" · {it.context}" if local and it.context else "")]
    if it.held:
        bits.append(f"HELD: {_ellipsize(it.held, 50)}")
    elif it.flags:
        bits.append(f"{len(it.flags)} flag{'s' if len(it.flags) != 1 else ''}")
    width = max(40, shutil.get_terminal_size((100, 30)).columns - 12)
    ticked = not it.held and it.meta.get("verdict") != "not_applicable"
    return Choice(_ellipsize(it.title, 48), "  ".join(b for b in bits if b), ticked, value=it,
                  more=_review_lines(it, local, width))


def _review_checklist(items: list[Learning], verb: str, rescrub, local: bool) -> list[Learning]:
    choices = [_review_choice(it, local) for it in items]

    def on_edit(c: Choice) -> None:
        if rescrub is None:
            return
        it = edit_in_editor(c.value, rescrub)
        fresh = _review_choice(it, local)
        c.label, c.detail, c.more = fresh.label, fresh.detail, fresh.more

    held = sum(1 for it in items if it.held)
    note = "→ shows a lesson in full (← hides it)."
    if any(it.meta.get("verdict") for it in items):
        note += " Each was checked against your notes; ones that don't apply to you start unticked."
    if rescrub:
        note += " e opens it in your editor; your edits are scrubbed again."
    if held:
        note += f" {held} held back start unticked (left out): read them before ticking."
    importing = verb.lower() == "import"
    title = "Which lessons do you want to import?" if importing else "Which lessons go in the bundle?"
    picked = checklist.run(title, choices, note=note, on_edit=on_edit if rescrub else None, more_label="read",
                           meaning=IMPORT_ITEM if importing else EXPORT_ITEM,
                           header=sensitivity.HEADER if local else "")
    return [c.value for c in picked]


def choose(question: str, options: list[str], default: int = 0, input_fn: InputFn | None = None) -> int:
    """Pick one of a few answers: arrow keys in a terminal, otherwise a number or first letter."""
    if input_fn is None and checklist.available():
        return checklist.choose(question, options, default=default)
    input_fn = input_fn or _input
    menu = "  ".join(f"[{i + 1}] {o}" for i, o in enumerate(options))
    while True:
        try:
            a = input_fn(f"{question}  {menu}  (Enter = {options[default]}): ").strip().lower()
        except EOFError:
            raise Aborted()
        if not a:
            return default
        if a.isdigit() and 1 <= int(a) <= len(options):
            return int(a) - 1
        hits = [i for i, o in enumerate(options) if o.lower().startswith(a)]
        if len(hits) == 1:
            return hits[0]
        if a in ("q", "quit"):
            raise Aborted()


def confirm(question: str, input_fn: InputFn | None = None, default: bool = True) -> bool:
    if input_fn is None and checklist.available():
        return checklist.choose(question, ["Yes", "No"], default=0 if default else 1) == 0
    input_fn = input_fn or _input
    try:
        a = input_fn(f"{question} {'[Y/n]' if default else '[y/N]'} ").strip().lower()
    except EOFError:
        return False
    return default if not a else a in ("y", "yes")


# ---------------------------------------------------------------------------------------------
# Curated lists (kungfu init): pick from suggestions, accept all, or add your own.


CURATE_HELP = """\
  Enter        accept: ticked ones get the [x] meaning shown above, the rest the [ ] meaning
  all / none   tick everything / nothing
  1,3-5        tick only these
  -2,7         untick these          +2,7   tick these too
  +Name, Name  add your own (comma-separated)
  q            quit without writing anything"""


def curate(
    title: str,
    choices: list[Choice],
    add: Callable[[str], list[Choice]] | None = None,
    input_fn: InputFn | None = None,
    out: TextIO | None = None,
    note: str = "",
    add_hint: str = "+Name, Name to add your own",
    meaning: Meaning | None = None,
    header: str = "",
) -> list[Choice]:
    """Show a ticked list; return the choices left ticked (including any the user added).

    In a terminal this is an arrow-key checklist; otherwise (scripts, tests) it takes typed commands.
    """
    out = out or sys.stdout
    if input_fn is None and out is sys.stdout and checklist.available():
        example = add_hint.lstrip("+").split(" to ")[0].strip()
        return checklist.run(title, choices, note=note, add=add, add_prompt=f"Add ({example}): ", meaning=meaning,
                             header=header)
    input_fn = input_fn or _input
    m = meaning or checklist.TICKED
    while True:
        print(f"\n{title}", file=out)
        if m is not checklist.TICKED:
            print(f"  [x] = {m.on}\n  [ ] = {m.off}", file=out)
        if note:
            print(f"  {note}", file=out)
        if not choices:
            print("  (nothing found)", file=out)
        width = max((len(c.label) for c in choices), default=0)
        width = min(width, 48)
        tag_w = max(len(m.on_tag), len(m.off_tag))
        if header and choices:
            print(" " * (2 + 3 + 1 + 3 + 2 + 1 + width + 2 + (tag_w + 2 if tag_w else 0)) + header, file=out)
        for i, c in enumerate(choices, 1):
            mark = "[x]" if c.selected else "[ ]"
            extra = "  (added)" if c.added else ""
            tag = f"{(m.on_tag if c.selected else m.off_tag):<{tag_w}}  " if tag_w else ""
            print(f"  {mark} {i:>3}. {_ellipsize(c.label, 48):<{width}}  {tag}{c.detail}{extra}", file=out)
        n_sel = sum(c.selected for c in choices)
        hint = (f"{n_sel} {m.on_count}, {len(choices) - n_sel} {m.off_count} · Enter accepts · all · none · "
                f"1,3-5 · -N untick · +N tick")
        hint += f" · {add_hint}" if add else ""
        try:
            answer = input_fn(f"{hint} · ? help: ").strip()
        except EOFError:
            raise Aborted()
        low = answer.lower()
        if not answer:
            return [c for c in choices if c.selected]
        if low in ("q", "quit"):
            raise Aborted()
        if low in ("?", "h", "help"):
            print(CURATE_HELP, file=out)
            continue
        if low in ("all", "a"):
            for c in choices:
                c.selected = True
            continue
        if low in ("none", "n"):
            for c in choices:
                c.selected = False
            continue
        if answer[0] in "+-":
            sel = parse_selection(answer[1:], len(choices)) if answer[1:].strip() else None
            if sel is not None:
                for i in sel:
                    choices[i].selected = answer[0] == "+"
                continue
            if answer[0] == "+" and add:
                new = add(answer[1:])
                if not new:
                    print("  Nothing to add there.", file=out)
                known = {c.label.lower() for c in choices}
                for c in new:
                    if c.label.lower() in known:
                        next(x for x in choices if x.label.lower() == c.label.lower()).selected = True
                    else:
                        c.added = True
                        choices.append(c)
                continue
            print("  Didn't understand that; ? for help.", file=out)
            continue
        sel = parse_selection(answer, len(choices))
        if sel is None:
            print("  Didn't understand that; ? for help.", file=out)
            continue
        for i, c in enumerate(choices):
            c.selected = i in sel
