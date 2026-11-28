# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""kungfu: export what you've learned, import what others have.

    kungfu init                      write a starter config
    kungfu scan                      show what would be offered for export, by topic
    kungfu export -o me.kungfu       pick topics, review, and pack a bundle
    kungfu inspect me.kungfu         look inside a bundle
    kungfu import me.kungfu --obsidian ~/Vault --wiki ~/wiki
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from types import SimpleNamespace
from datetime import date
from pathlib import Path

from kungfu import __version__, bundle, config, frontmatter, locate, rain, risk, select, sensitivity, topics
from kungfu.extract import Plan, run
from kungfu.model import Learning
from kungfu.sanitize import Redactor, residual_secrets
from kungfu.sources import instructions
from kungfu.sources import llm_wiki as wiki_src
from kungfu.sources import obsidian as obs_src


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# ---------------------------------------------------------------------------------------------
# init


def _existing_config() -> dict:
    import tomllib

    path = config.config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _vault_folders(vaults: list[Path], deny: list[str]) -> list[tuple[str, int]]:
    deny_l = {d.lower() for d in deny}
    folders: dict[str, int] = {}
    for v in vaults:
        if not v.is_dir():
            continue
        for d in sorted(p for p in v.iterdir() if p.is_dir() and not p.name.startswith(".")):
            if d.name.lower() in deny_l:
                continue
            folders[d.name] = folders.get(d.name, 0) + sum(1 for _ in d.rglob("*.md"))
    return list(folders.items())


def _kb_kind(p: Path) -> str | None:
    if wiki_src.is_wiki(p):
        return "LLM wiki" + (" (in Obsidian)" if obs_src.is_vault(p) else "")
    if obs_src.is_vault(p):
        return "Obsidian vault"
    return None


def _short(p: Path, width: int = 48) -> str:
    label = str(p).replace(str(Path.home()), "~", 1)
    return label if len(label) <= width else "…" + label[-(width - 1):]  # the end tells folders apart


def _pick_repo_folders(roots: list[str], groups: dict, mine: set) -> list[str]:
    """Offer every folder found holding git repos, ticked, plus configured ones; return the chosen."""
    configured = [Path(config.clean_path(r)) for r in roots]
    candidates = list(dict.fromkeys(configured + list(groups)))

    def choice(p: Path, selected: bool = True) -> select.Choice:
        repos = groups.get(p)
        if repos is None:
            detail = "from your config" if p.is_dir() else "not found"
        else:
            yours = sum(1 for r in repos if r in mine)
            kind = "repo" if repos == [p] else f"{len(repos)} repo{'s' if len(repos) != 1 else ''}"
            detail = f"{kind} · {yours} with your commits" if yours else f"{kind} · none yours (third-party clones are skipped)"
        return select.Choice(_short(p), detail, selected and p.is_dir(), value=p)

    def add(text: str) -> list[select.Choice]:
        out = []
        for raw in text.split(","):
            p = Path(config.clean_path(raw)) if raw.strip() else None
            if p and p.is_dir():
                out.append(choice(p))
            elif p:
                print(f"  Not found: {p}")
        return out

    picked = select.curate(
        "Which folders of git repos should be read for learnings (CLAUDE.md, AGENTS.md, rules, skills)?",
        [choice(p) for p in candidates], add=add, add_hint="+/path to add a folder", meaning=select.READ_REPOS,
        note="Found by searching your home folder and iCloud. Even in a ticked folder, only repos you've "
             "committed to are read.")
    return [str(c.value) for c in picked]


def _pick_knowledge_bases(vaults: list[str], wikis: list[str], found=None) -> tuple[list[str], list[str]]:
    """Offer every vault and wiki found (ticked) plus configured ones; return (vaults, wikis)."""
    home = str(Path.home())
    configured = [Path(config.clean_path(p)) for p in vaults + wikis]
    discovered = (list(found.wikis) + list(found.vaults)) if found else []
    candidates: list[Path] = []
    for p in configured + discovered + config.known_vaults():
        if p not in candidates:
            candidates.append(p)
    configured = candidates  # everything found starts ticked

    def choice(p: Path, selected: bool) -> select.Choice:
        kind = _kb_kind(p) or ("not found" if not p.exists() else "not a vault or wiki")
        notes = sum(1 for _ in obs_src.notes(p)) if p.is_dir() else 0
        nested = " · has vaults inside, which are listed separately" if p.is_dir() and any(
            (d / ".obsidian").is_dir() for d in p.iterdir() if d.is_dir()) else ""
        return select.Choice(_short(p), f"{kind} · {notes} notes{nested}", selected and p.exists(), value=p)

    def add(text: str) -> list[select.Choice]:
        out = []
        for raw in text.split(","):
            if not raw.strip():
                continue
            p = Path(config.clean_path(raw))
            if not p.is_dir():
                print(f"  Not found: {p}")
            elif not _kb_kind(p):
                print(f"  {p} has no .obsidian folder and isn't an LLM wiki (wiki/ + index.md or log.md).")
            else:
                out.append(choice(p, True))
        return out

    picked = select.curate(
        "Which Obsidian vaults and LLM wikis should learnings be exported from?",
        [choice(p, p in configured) for p in candidates], add=add,
        add_hint="+/path/to/vault to add one", meaning=select.READ_KB,
        note="Found by searching your home folder and iCloud, and from Obsidian's vault list. Vault notes "
             "still need a #kungfu tag (or an included folder, next) before they're offered.")
    new_vaults, new_wikis = [], []
    for c in picked:
        p = c.value if c.value is not None else Path(config.clean_path(c.label))
        (new_wikis if wiki_src.is_wiki(p) else new_vaults).append(str(p))
    return new_vaults, new_wikis


FORMER_EXPLAINED = (
    "Former names, such as a deadname or a previous legal name, plus old usernames and email\n"
    "addresses. They're replaced with \"the user\" (exactly like your current name), anything that\n"
    "mentions them is held back, and they're never sent to Claude. They're saved only as salted\n"
    "hashes, in {path} (readable only by you), never as text."
)


def _read_former_names(prompt: str) -> list[str]:
    """Read entries one per line (shown as you type them); an empty line finishes."""
    entries = []
    if not sys.stdin.isatty():
        return [ln.strip() for ln in sys.stdin if ln.strip()]
    while True:
        try:
            raw = select._input(prompt)
        except EOFError:
            break
        if not raw.strip():
            break
        entries.append(raw.strip())
    return entries


def _former_names_step() -> None:
    from kungfu import former_names

    former = former_names.FormerNames.load()
    n = former.entries
    if n:
        pick = select.choose(f"\nYou have {n} former name{'s' if n != 1 else ''} saved (never shown).",
                             ["Keep them", "Add more", "Remove all"])
        if pick == 0:
            return
        if pick == 2:
            former.clear()
            former.save()
            print("  Removed.")
            return
    else:
        pick = select.choose("\nDo you have former names (such as a deadname) that must never appear in an export?",
                             ["No", "Yes, add them"], default=0)
        if pick == 0:
            return
    print(FORMER_EXPLAINED.format(path=_short(former_names.default_path(), 80)))
    print("Type each name, username or email address, then Enter. Press Enter on an empty line when done.")
    print("A full name also covers its words and usernames made from it (Jordan Example → jexample).")
    added = 0
    for entry in _read_former_names("  Former name: "):
        former.add(entry)
        added += 1
    if added:
        former.save()
        former_names.forget_cache()
        print(f"  Saved {added}; {former.entries} in all.")


def cmd_former_names(args) -> int:
    from kungfu import former_names

    former = former_names.FormerNames.load()
    if args.clear:
        former.clear()
        former.save()
        print("Removed all former names.")
        return 0
    if args.count:
        print(former.entries)
        return 0
    if sys.stdin.isatty():
        print(FORMER_EXPLAINED.format(path=_short(former_names.default_path(), 80)))
        print("Type each one, then Enter; an empty line finishes. (Or pipe them in, one per line.)")
    entries = _read_former_names("  Former name: ")
    for e in entries:
        former.add(e)
    if entries:
        former.save()
    print(f"Saved {len(entries)}; {former.entries} in all.")
    return 0


def _init_refused(args) -> int | None:
    """Why init won't run, checked before the risk warning so a refusal doesn't cost an acknowledgement."""
    if _existing_config() and not args.force:
        err(f"{config.config_path()} already exists. Run `kungfu init --force` to go through it again; "
            "your current choices are kept as the starting point.")
        return 1
    return None


def cmd_init(args) -> int:
    from kungfu.discover import CATEGORY_TITLES, REPLACEMENT, discover, looks_like_a_word

    if (refused := _init_refused(args)) is not None:
        return refused
    path = config.config_path()
    old = _existing_config()
    ask = interactive() and not args.defaults and not args.accept_all
    if not interactive() and not (args.defaults or args.accept_all):
        args.defaults = True
        err("No terminal to ask on: writing defaults without discovery (use --accept-all to discover and accept everything).")

    src, red, top, obs = old.get("sources", {}), old.get("redact", {}), old.get("topics", {}), old.get("obsidian", {})
    home = str(Path.home())
    roots = list(src.get("repo_roots") or [])
    vaults = list(dict.fromkeys(list(src.get("obsidian_vaults", [])) + list(args.vault or [])))
    wikis = list(dict.fromkeys(list(src.get("llm_wikis", [])) + list(args.wiki or [])))
    include = list(obs.get("include_folders", []))
    own = {k.lower() for k, v in config.auto_redact_terms().items() if v == "the user"}
    terms: dict[str, str] = {str(k): str(v) for k, v in red.get("terms", {}).items()
                             if not (str(k).lower().lstrip("@") in own and str(v) == "[person]")}
    keep: list[str] = [str(k) for k in red.get("keep", [])]
    cased: dict[str, str] = {str(k): str(v) for k, v in red.get("cased_terms", {}).items()}
    exclude: list[str] = [str(t) for t in top.get("exclude", [])]
    custom: dict[str, list[str]] = {str(k): list(v) for k, v in top.get("custom", {}).items()}
    only_mine = bool(src.get("only_my_repos", True))

    if not args.defaults and not _claude_ready(config.load().distill_model):
        return 1
    try:
        vaults = [config.clean_path(v) for v in vaults]
        wikis = [config.clean_path(w) for w in wikis]
        roots = [config.clean_path(r) for r in roots]
        found, groups, mine = None, {}, set()
        if not args.defaults:
            with rain.activity("Searching your home folder and iCloud for git repos, Obsidian vaults and LLM wikis"):
                found = locate.locate()
                groups = found.repo_folders()
                mine = set(instructions.my_repos(found.repos, config.user_identities()))
            if found.truncated:
                err("note: the search stopped at its time limit, so some folders may be missing; add them with +path.")
        if ask:
            print("Setting up I Know Kung-Fu.")
            roots = _pick_repo_folders(roots, groups, mine)
            vaults, wikis = _pick_knowledge_bases(vaults, wikis, found)
            _former_names_step()
        elif found:  # --accept-all
            roots = list(dict.fromkeys(roots + [str(p) for p in groups]))
            vaults = list(dict.fromkeys(vaults + [str(p) for p in found.vaults + config.known_vaults()
                                                  if p not in found.wikis]))
            wikis = list(dict.fromkeys(wikis + [str(p) for p in found.wikis]))
        if not roots:
            roots = [r for r in config.DEFAULT_REPO_ROOTS if Path(r).expanduser().is_dir()]
        home_s = str(Path.home())
        roots = [r.replace(home_s, "~", 1) if r.startswith(home_s) else r for r in roots]

        cfg = config.load()
        cfg.repo_roots = [Path(r).expanduser() for r in roots]
        cfg.vaults = [Path(v).expanduser() for v in vaults]
        cfg.wikis = [Path(w).expanduser() for w in wikis]
        cfg.only_my_repos = only_mine

        folders = _vault_folders(cfg.vaults, cfg.deny_folders)
        if ask and folders:
            choices = [select.Choice(name, f"{n} notes", name in include) for name, n in folders]
            picked = select.curate(
                "Which vault folders should offer every note, not just #kungfu-tagged ones?",
                choices, meaning=select.VAULT_FOLDER,
                note="Tick a folder only if everything in it is fine to share. People, Daily, Meetings and "
                     "similar folders are never offered either way.")
            include = [c.label for c in picked]
        cfg.include_folders = include
        cfg.keep_terms = []

        if args.defaults:
            findings = None
        else:
            with rain.activity("Scouring your sources for names, organizations, projects and topics"):
                findings = discover(cfg, Plan(vaults=list(cfg.vaults), wikis=list(cfg.wikis)))
            for e in findings.errors:
                err(f"warning: {e}")

        if findings:
            for cat, title in CATEGORY_TITLES.items():
                cands = findings.candidates.get(cat, [])
                repl = REPLACEMENT[cat]
                choices = [select.Choice(c.term, c.describe(),
                                         selected=(c.term in terms or c.term in cased)
                                         or (c.suggested and c.term not in keep),
                                         value=c) for c in cands]
                # Terms you added before that discovery didn't find again.
                for t, r in {**terms, **cased}.items():
                    if r == repl and t.lower() not in {c.term.lower() for c in cands}:
                        # Capitalized-only entries ("Smith") are already safe; case-insensitive ones that
                        # look like words ("Metadata") are probably stale suggestions.
                        # Carry its list (capitalized-only or not) with it.
                        kept = SimpleNamespace(cased=t in cased, auto=False)
                        if cat == "person" and t in terms and looks_like_a_word(t):
                            choices.append(select.Choice(
                                t, "from your config · looks like an ordinary word, not a name, so unticked", False,
                                value=kept))
                        else:
                            choices.append(select.Choice(t, "from your config", True, value=kept))
                note = ("Unticking one marked \"detected automatically\" means it's never scrubbed, "
                        "even though kungfu would otherwise scrub it.")
                if cat == "project":
                    note = ("Ticked: names of your repos and Claude projects. Unticked: other capitalized names "
                            "found in your notes; tick the internal ones (codenames, internal systems). Public "
                            "products can stay as written.")
                if cat == "other":
                    note = ("These could be people, places or products; kungfu can't tell from a single word. "
                            "Tick the ones that are people or internal names. Places and public products can "
                            "stay as written.")
                if cat == "acronym":
                    note = ("Most acronyms are ordinary tech terms and should stay as written; tick only "
                            "your own teams, programs or internal systems.")
                if findings.unmentioned_auto.get(cat):
                    note += (f" {findings.unmentioned_auto[cat]} more are scrubbed automatically; they don't appear in "
                             "anything exportable, so they aren't listed.")
                if ask:
                    picked = select.curate(f"{title}: which should be scrubbed from exports?", choices, note=note,
                                           meaning=select.scrub(repl),
                                           add=lambda text: [select.Choice(x.strip(), "added by you", True)
                                                             for x in text.split(",") if x.strip()])
                elif args.accept_all:
                    picked = choices
                else:
                    picked = [c for c in choices if c.selected]
                picked_l = {c.label.lower() for c in picked}
                for c in choices:
                    cased_term = c.value is not None and getattr(c.value, "cased", False)
                    if c.label.lower() in picked_l:
                        (cased if cased_term else terms)[c.label] = repl
                        (terms if cased_term else cased).pop(c.label, None)
                        keep = [k for k in keep if k.lower() != c.label.lower()]
                    else:
                        terms.pop(c.label, None)
                        cased.pop(c.label, None)
                        if c.value is not None and c.value.auto and c.label not in keep:
                            keep.append(c.label)  # auto-detected but you said it's fine to show
                for c in picked:
                    if c.value is None:
                        terms[c.label] = repl

            # Topics, scored with the terms just chosen.
            cfg.redact_terms = {**config.auto_redact_terms(), **terms}
            cfg.cased_terms = dict(cased)
            cfg.keep_terms = keep
            cfg.exclude_topics = []
            cfg.custom_topics = custom
            with rain.activity("Sorting learnings into topics and scoring sensitivity"):
                result = run(cfg, Plan(vaults=list(cfg.vaults), wikis=list(cfg.wikis)))
            mode = sensitivity.color_mode(sys.stdout)
            choices = [select.Choice(t, f"{n:>3} items  {sensitivity.gauge(sensitivity.topic_score(its), mode)}",
                                     t not in exclude, value=t)
                       for t, n, its in topics.counts(result.offered)]
            for name, kws in custom.items():
                if name not in {c.label for c in choices}:
                    choices.append(select.Choice(name, "yours: " + ", ".join(kws), name not in exclude, value=name))

            def add_topic(text: str) -> list:
                name, _, kws = text.partition(":")
                words = [w.strip() for w in kws.split(",") if w.strip()]
                if not name.strip() or not words:
                    print("  Add a topic as +name: keyword, keyword", file=sys.stdout)
                    return []
                custom[topics.normalize(name)] = words
                return [select.Choice(topics.normalize(name), "yours: " + ", ".join(words), True)]

            if ask:
                picked = select.curate(
                    "Which topics may ever be exported?", choices, meaning=select.OFFER_TOPIC,
                    add=add_topic, add_hint="+name: keyword, keyword to add a topic",
                    note="An unticked topic is never offered by `kungfu export`.",
                    header=" " * len("  9 items  ") + sensitivity.HEADER)
            else:
                picked = choices
            picked_l = {c.label for c in picked}
            exclude = sorted({c.label for c in choices if c.label not in picked_l})
    except select.Aborted:
        err("\nCancelled; nothing written.")
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.render_template(roots, vaults, wikis, include, terms, keep=keep, exclude=exclude,
                                           cased=cased,
                                           custom=custom, only_my_repos=only_mine), encoding="utf-8")
    path.chmod(0o600)
    counts = Counter(list(terms.values()) + list(cased.values()))
    summary = ", ".join(f"{n} {r}" for r, n in counts.most_common())
    print(f"\nWrote {path}.")
    if terms:
        print(f"  Scrubbing: {summary}.")
    if keep:
        print(f"  Never scrubbing: {', '.join(keep)}.")
    if exclude:
        print(f"  Topics never exported: {', '.join(exclude)}.")
    print("Next: `kungfu scan` to see what would be exported.")
    return 0


def cmd_discover(args) -> int:
    from kungfu.discover import CATEGORY_TITLES, REPLACEMENT, discover

    cfg = config.load()
    with rain.activity("Looking for names, organizations, projects and domains"):
        findings = discover(cfg, build_plan(args, cfg))
    if args.json:
        print(json.dumps({cat: [{"term": c.term, "replacement": c.replacement, "count": c.count,
                                 "evidence": sorted(c.evidence), "auto": c.auto, "suggested": c.suggested,
                                 "configured": c.term in cfg.redact_terms}
                                for c in cs] for cat, cs in findings.candidates.items()}, indent=2))
        return 0
    for cat, title in CATEGORY_TITLES.items():
        cands = findings.candidates.get(cat, [])
        print(f"\n{title} → {REPLACEMENT[cat]}")
        for c in cands:
            mark = "✓" if c.term in cfg.redact_terms or c.auto else ("·" if c.suggested else " ")
            print(f"  {mark} {c.term:<32} {c.describe()}")
        if not cands:
            print("    (none found)")
        if findings.unmentioned_auto.get(cat):
            print(f"    + {findings.unmentioned_auto[cat]} more scrubbed automatically, not mentioned in exportable text")
    print("\n✓ already scrubbed (automatically or by your config) · suggested")
    print("Run `kungfu init --force` to choose which to scrub.")
    return 0


# ---------------------------------------------------------------------------------------------
# shared plumbing


def build_plan(args, cfg: config.Config) -> Plan:
    if args.claude_dir:
        cfg.claude_dir = Path(args.claude_dir).expanduser()
    if args.repos_root:
        cfg.repo_roots = [Path(p).expanduser() for p in args.repos_root]
    if args.include_folder:
        cfg.include_folders += args.include_folder
    vaults = [Path(v).expanduser() for v in (args.vault or [])] or list(cfg.vaults)
    wikis = [Path(w).expanduser() for w in (args.wiki or [])] or list(cfg.wikis)
    return Plan(claude=not args.no_claude, repos=not args.no_repos, vaults=vaults, wikis=wikis)


def held_by_topic(items: list[Learning]) -> dict[str, int]:
    c: Counter = Counter()
    for it in items:
        if it.held:
            c.update(it.topics)
    return dict(c)


def choose_topics(args, items: list[Learning], verb: str, held: dict[str, int] | None = None,
                  show_sensitivity: bool = False) -> list[str] | None:
    rows = topics.counts(items)
    available = [t for t, _, _ in rows]
    if args.all_topics:
        return available
    if args.topics:
        wanted = [topics.normalize(t) for t in args.topics.split(",") if t.strip()]
        missing = [t for t in wanted if t not in available]
        if missing:
            err(f"Not in this set: {', '.join(missing)}. Available: {', '.join(available)}")
            return None
        return wanted
    if not interactive():
        err("Choose topics with --topics a,b,c or --all-topics (no terminal to ask on).")
        err("Available: " + ", ".join(f"{t} ({n})" for t, n, _ in rows))
        return None
    return select.pick_topics(rows, verb, held_counts=held, show_sensitivity=show_sensitivity,
                              preselect=(verb == "import"))


# ---------------------------------------------------------------------------------------------
# scan


def cmd_scan(args) -> int:
    cfg = config.load()
    with rain.activity("Scanning your memories, instruction files, vaults and wikis"):
        result = run(cfg, build_plan(args, cfg))
    if args.json:
        print(json.dumps({
            "sources": result.sources,
            "errors": result.errors,
            "learnings": [
                {"id": i.id, "title": i.title, "topics": i.topics, "kind": i.kind, "source_type": i.source_type,
                 "context": i.context, "origin": i.origin, "held": i.held, "flags": i.flags,
                 "sensitivity": sensitivity.score(i)}
                for i in result.items
            ],
            "topics": {t: {"items": n, "sensitivity": sensitivity.topic_score(its)}
                       for t, n, its in topics.counts(result.offered)},
        }, indent=2))
        return 0
    print("Sources:")
    for label, n in result.sources.items():
        print(f"  {n:>4}  {label}")
    for e in result.errors:
        print(f"  !  {e}")
    for h in result.hints:
        print(f"  →  {h}")
    offered, held = result.offered, result.held
    print(f"\n{len(offered)} learning(s) offered for export, {len(held)} held back.\n")
    hb = held_by_topic(result.items)
    rows = topics.counts(offered)
    mode = sensitivity.color_mode(sys.stdout)
    width = max((len(t) for t, _, _ in rows), default=5)
    print(f"  {'':<{width}}  {'':>5}  {sensitivity.HEADER}")
    print(f"  {'topic':<{width}}  {'items':>5}  {'sensitivity':<15} held")
    for topic, n, its in rows:
        g = sensitivity.gauge(sensitivity.topic_score(its), mode)
        print(f"  {topic:<{width}}  {n:>5}  {g}  " + (f"+{hb[topic]}" if hb.get(topic) else ""))
    print("\n  held = learnings held back by default; not counted in items or sensitivity.")
    if held:
        print("\nHeld back, by reason:")
        reasons = Counter(_reason_label(h.held) for h in held)
        for r, n in reasons.most_common():
            print(f"  {n:>4}  {r}")
    if args.list:
        print(f"\nEvery learning ({sensitivity.HEADER}):")
        for it in sorted(result.items, key=lambda i: (bool(i.held), i.topics[0] if i.topics else "", i.title)):
            mark = "HELD" if it.held else "    "
            print(f"\n{mark} {it.title}\n     {', '.join(it.topics)} · {it.source_type} · {it.context}")
            print(f"     sensitivity {sensitivity.gauge(sensitivity.score(it), mode)}")
            if it.flags:
                print(f"     flags: {', '.join(it.flags)}")
            if it.held:
                print(f"     reason: {it.held}")
    print("\nNext: `kungfu export` to choose topics and build a bundle.")
    return 0


# ---------------------------------------------------------------------------------------------
# export


def _claude_ready(model: str) -> bool:
    """A working Anthropic API connection is a prerequisite for export, import and init."""
    from kungfu import claude

    try:
        with rain.activity("Checking the connection to Claude"):
            claude.preflight(model)
    except claude.Unavailable as exc:
        err(str(exc))
        return False
    return True


def _warn_noisy_terms(fired: Counter) -> None:
    """A scrub term that keeps replacing words in the lessons is often a word, not a name."""
    from kungfu.discover import looks_like_a_word

    noisy = [(t, n, r) for (t, r), n in fired.most_common() if n >= 5 or looks_like_a_word(t)]
    if not noisy:
        return
    print("\nThese scrub terms replaced text in the lessons; check they're really names, not ordinary words:")
    for t, n, r in noisy[:10]:
        print(f"  “{t}” → {r}  ×{n}" + ("   (looks like an ordinary word)" if looks_like_a_word(t) else ""))
    print("  If one isn't a name, untick it in `kungfu init --force` (or delete its line from config.toml) and "
          "export again.")


def cmd_export(args) -> int:
    from kungfu import claude, lessons

    cfg = config.load()
    if not _claude_ready(cfg.distill_model):
        return 1
    plan = build_plan(args, cfg)
    with rain.activity("Scanning your memories, instruction files, vaults and wikis"):
        result = run(cfg, plan)
    for e in result.errors:
        err(f"warning: {e}")
    for h in result.hints:
        err(f"note: {h}")
    pool = result.offered  # held-back learnings are never sent to Claude
    if not pool:
        err("Nothing to export. Run `kungfu scan` to see what was found and why it was held back.")
        return 1
    print(f"Found {len(result.offered)} learning(s) to offer ({len(result.held)} held back) from "
          f"{sum(result.sources.values())} candidates.", file=sys.stderr)
    try:
        chosen = choose_topics(args, pool, "export", held_by_topic(result.items), show_sensitivity=True)
        if chosen is None:
            return 2
        selected = topics.select(pool, set(chosen))
        if not selected:
            err("No learnings in those topics.")
            return 1
        print(f"\n{len(selected)} learning(s) in {len(chosen)} topic(s):")
        mode = sensitivity.color_mode(sys.stdout)
        width = max(len(t) for t in chosen)
        print(f"  {'':<{width}}  {'':>4}  {sensitivity.HEADER}")
        for t in chosen:
            its = [i for i in selected if t in i.topics]
            print(f"  {t:<{width}}  {len(its):>4}  {sensitivity.gauge(sensitivity.topic_score(its), mode)}")
        if interactive() and not args.yes and not select.confirm(
                f"Send these {len(selected)} scrubbed learning(s) to Claude ({cfg.distill_model}) to write lessons?"):
            err("Nothing sent; nothing written.")
            return 1
        with rain.activity(f"Writing lessons from {len(selected)} learning(s)") as r:
            written, skipped = lessons.synthesize(selected, cfg.distill_model, progress=r.status)
        # Claude's lessons go through the same scrubbing and content checks as everything else.
        fired: Counter = Counter()
        for le in written:
            fired.update(result.redactor.term_hits(f"{le.title}\n{le.summary}\n{le.body}"))
            result.redactor.apply(le)
        _warn_noisy_terms(fired)
        if skipped:
            print(f"\n{len(skipped)} learning(s) didn't become lessons:")
            for it, why in skipped[:12]:
                print(f"  - {it.title[:50]}: {why}")
            if len(skipped) > 12:
                print(f"  … and {len(skipped) - 12} more")
        if not written:
            err("Claude wrote no lessons from these learnings; nothing written.")
            return 1
        print(f"\n{len(written)} lesson(s) from {len(selected) - len(skipped)} learning(s).")
        if interactive() and not args.yes:
            written = select.review(written, "Export", rescrub=result.redactor.apply)
        else:
            written = [le for le in written if not le.held]
    except claude.Unavailable as exc:
        err(str(exc))
        return 1
    except select.Aborted:
        err("\nCancelled; nothing written.")
        return 1

    if not written:
        err("Nothing selected; nothing written.")
        return 1
    blocked = [s for s in written if residual_secrets(f"{s.title}\n{s.summary}\n{s.body}")]
    if blocked:
        for s in blocked:
            err(f"refusing: “{s.title}” still contains something that looks like a secret or a personal "
                f"identifier ({', '.join(residual_secrets(f'{s.title}{chr(10)}{s.summary}{chr(10)}{s.body}'))}).")
        return 1
    if cfg.former and any(cfg.former.find(f"{s.title}\n{s.summary}\n{s.body}") for s in written):
        err("refusing: a lesson still contains one of your former names. Edit it out and try again.")
        return 1
    out = Path(args.output or f"kungfu-{date.today().isoformat()}.kungfu")
    title = args.title or f"Kung-Fu lessons {date.today().isoformat()}"
    manifest = bundle.write(out, written, title=title, description=args.description or "", author=args.author,
                            content="lessons")
    print(f"\nTank: loading {len(written)} program(s) into {out}")
    print("Topics: " + ", ".join(f"{t} ({n})" for t, n in manifest["topics"].items()))
    print(f"Look inside with `kungfu inspect {out}` before you send it.")
    return 0


# ---------------------------------------------------------------------------------------------
# inspect & import


def cmd_inspect(args) -> int:
    try:
        b = bundle.read(Path(args.bundle), strict=False)
    except bundle.BundleError as exc:
        err(str(exc))
        return 1
    m = b.manifest
    print(f"{b.title}" + (f" — by {m['author']}" if m.get("author") else ""))
    kind = "lesson(s)" if b.lessons else "raw learning(s)"
    made_by = f"kungfu {b.version}" if b.version else "an older kungfu (version not recorded)"
    print(f"created {m.get('created')} with {made_by} · {len(b.items)} {kind}")
    if b.version != __version__:
        print(f"⚠ This is kungfu {__version__}, so this bundle can't be imported here: "
              "ask the sender to export it again with this version.")
    elif not b.lessons:
        print("⚠ This bundle holds raw learnings, not lessons, so it can't be imported.")
    if m.get("description"):
        print(m["description"])
    if args.show:
        for i, it in enumerate(b.items, 1):
            if args.show in (str(i), it.id):
                select.show_item(it, sys.stdout, full=True, local=False)
                return 0
        err(f"No learning {args.show!r}.")
        return 1
    print("\nTopics:")
    for t, n in m.get("topics", {}).items():
        print(f"  {t:<24} {n:>4}")
    print("\nLearnings:")
    for i, it in enumerate(b.items, 1):
        conf = f" · {it.meta['confidence']} confidence" if it.meta.get("confidence") else ""
        print(f"  {i:>3}. {it.title}  [{', '.join(it.topics)}{conf}]")
        if it.meta.get("statement"):
            print(f"       {it.meta['statement'][:110]}")
    print(f"\nShow one with `kungfu inspect {args.bundle} --show N`.")
    return 0


def _reason_label(held: str) -> str:
    """A held-back reason without its specifics, for grouping: "held back: talks about someone (x)"
    -> "talks about someone"."""
    text = re.sub(r"^held back:\s*", "", held)
    return text.split(";")[0].split(":")[0].split("(")[0].strip() or "held back"


def _ellipsize_line(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def cmd_import(args) -> int:
    from kungfu import claude, examine
    from kungfu.importers import llm_wiki as wiki_imp
    from kungfu.importers import obsidian as obs_imp

    obsidian_targets = [Path(p).expanduser() for p in (args.obsidian or [])]
    wiki_targets = [Path(p).expanduser() for p in (args.wiki or [])]
    for p in (Path(x).expanduser() for x in (args.into or [])):
        if wiki_src.is_wiki(p):
            wiki_targets.append(p)
        elif obs_src.is_vault(p):
            obsidian_targets.append(p)
        else:
            err(f"{p} is neither an Obsidian vault (.obsidian/) nor an LLM wiki (wiki/ + index.md/log.md); "
                "use --obsidian or --wiki to say which.")
            return 2
    if not obsidian_targets and not wiki_targets:
        err("Where to? Give --obsidian VAULT and/or --wiki WIKI (or --into PATH to detect).")
        return 2
    for t in obsidian_targets + wiki_targets:
        if not t.is_dir():
            err(f"{t} doesn't exist.")
            return 2
    try:
        b = bundle.read(Path(args.bundle))
    except bundle.BundleError as exc:
        err(str(exc))
        return 1
    if not b.lessons:
        err("This bundle holds raw learnings, not lessons, and kungfu only imports lessons written and "
            "checked by Claude. Ask the sender to export it again with this version of kungfu.")
        return 1
    cfg = config.load()
    if not _claude_ready(cfg.distill_model):
        return 1
    m = b.manifest
    print(f"{b.title}" + (f" — by {m['author']}" if m.get("author") else "") +
          f" · {len(b.items)} lesson(s), created {m.get('created')}")
    try:
        chosen = choose_topics(args, b.items, "import")
        if chosen is None:
            return 2
        items = topics.select(b.items, set(chosen))
        if not items:
            err("No lessons in those topics.")
            return 1
        # Re-examine each lesson against the recipient's own notes before anything is written.
        target = (obsidian_targets + wiki_targets)[0]
        notes = examine.index(target, skip_folder=args.folder if target in obsidian_targets else None)
        related_count = len({n.path for le in items for n, _ in examine.related(le, notes)})
        if interactive() and not args.yes and not select.confirm(
                f"Send {len(items)} lesson(s) and excerpts of {related_count} of your related note(s) to Claude "
                f"({cfg.distill_model}) to check them against what you already know?"):
            err("Nothing sent; nothing written. (Lessons are only imported after they've been examined.)")
            return 1
        with rain.activity(f"Examining {len(items)} lesson(s) against your notes") as r:
            examine.examine(items, notes, cfg.distill_model, target, progress=r.status)
        counts = Counter(le.meta.get("verdict", "unexamined") for le in items)
        print("Examined: " + ", ".join(f"{n} {v.replace('_', ' ')}" for v, n in counts.most_common()))
        if interactive() and not args.yes:
            items = select.review(items, "Import", local=False)
        else:
            skipped = [le for le in items if le.meta.get("verdict") == "not_applicable"]
            if skipped:
                print(f"Left out {len(skipped)} lesson(s) that don't apply to you:")
                for le in skipped:
                    print(f"  - {le.title}: {_ellipsize_line(le.meta.get('explanation', ''), 150)}")
            items = [le for le in items if le.meta.get("verdict") != "not_applicable"]
    except claude.Unavailable as exc:
        err(str(exc))
        return 1
    except select.Aborted:
        err("\nCancelled; nothing written.")
        return 1
    if not items:
        err("Nothing selected; nothing written.")
        return 1

    reports = []
    with rain.activity(f"Loading {len(items)} lesson(s)"):
        _load_all(args, b, items, obsidian_targets, wiki_targets, reports, obs_imp, wiki_imp)
    _report_import(args, reports)
    return 0


def _load_all(args, b, items, obsidian_targets, wiki_targets, reports, obs_imp, wiki_imp) -> None:
    for vault in obsidian_targets:
        reports.append((f"Obsidian vault {vault}", obs_imp.load(b, items, vault, folder=args.folder,
                                                                update=args.update, dry_run=args.dry_run,
                                                                ack=args.acknowledgement)))
    for wiki in wiki_targets:
        reports.append((f"LLM wiki {wiki}", wiki_imp.load(b, items, wiki, direct=args.direct,
                                                          update=args.update, dry_run=args.dry_run,
                                                          ack=args.acknowledgement)))


def _report_import(args, reports) -> None:
    prefix = "[dry run] would write" if args.dry_run else "wrote"
    warned: set[str] = set()
    for label, r in reports:
        print(f"\n{label}: {prefix} {len(r.written)}, updated {len(r.updated)}, "
              f"skipped {len(r.skipped)} already present")
        if args.verbose or args.dry_run:
            for p in r.written:
                print(f"    + {p}")
            for p in r.updated:
                print(f"    ~ {p}")
        for w in r.warnings:
            if w not in warned:
                warned.add(w)
                print(f"  ! {w}")
        for n in r.notes:
            print(f"  {n}")
    if not args.dry_run and any(r.written or r.updated for _, r in reports):
        print("\nI know kung fu.")


# ---------------------------------------------------------------------------------------------


def cmd_locate(args) -> int:
    with rain.activity("Searching your home folder and iCloud for git repos, Obsidian vaults and LLM wikis"):
        found = locate.locate()
        groups = found.repo_folders()
        mine = set(instructions.my_repos(found.repos, config.user_identities()))
    print(f"Searched in {found.seconds:.1f}s" + (" (stopped at the time limit; results are partial)" if found.truncated else ""))
    print(f"\nFolders with git repos ({len(found.repos)} repos, {len(mine)} with your commits):")
    for p, repos in groups.items():
        yours = sum(1 for r in repos if r in mine)
        print(f"  {_short(p, 70):<70}  {len(repos):>3} repo{'s' if len(repos) != 1 else ' '}, {yours} yours")
    for title, paths in (("Obsidian vaults", found.vaults), ("LLM wikis", found.wikis)):
        print(f"\n{title}:")
        for p in paths:
            print(f"  {_short(p, 70)}")
        if not paths:
            print("  (none found)")
    print("\n`kungfu init` offers all of these, ticked.")
    return 0


def cmd_rain(args) -> int:
    import time

    r = rain.Rain(args.message, delay=0, height=args.height)
    if not r.enabled:
        err("The rain only falls in a terminal (and not with KUNGFU_RAIN=0).")
        return 1
    with r:
        try:
            time.sleep(args.seconds)
        except KeyboardInterrupt:
            pass
    print("Whoa.")
    return 0


def _source_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("sources (default: from config)")
    g.add_argument("--claude-dir", help="Claude Code home (default ~/.claude)")
    g.add_argument("--repos-root", action="append", metavar="DIR", help="folder of git repos; repeatable")
    g.add_argument("--vault", action="append", metavar="DIR", help="Obsidian vault; repeatable")
    g.add_argument("--wiki", action="append", metavar="DIR", help="LLM wiki; repeatable")
    g.add_argument("--include-folder", action="append", metavar="NAME",
                   help="vault folder whose notes are offered without a #kungfu tag; repeatable")
    g.add_argument("--no-claude", action="store_true", help="skip Claude memories and ~/.claude instructions")
    g.add_argument("--no-repos", action="store_true", help="skip git repositories")


def _topic_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--topics", help="comma-separated topics, instead of choosing interactively")
    p.add_argument("--all-topics", action="store_true", help="take every topic")


EPILOG = """\
typical use:
  kungfu init                              choose what to scrub (names, orgs, projects) and which topics to offer
  kungfu scan                              see what would be exported, by topic, with sensitivity
  kungfu export -o me.kungfu               pick topics; Claude writes lessons; review and pack them
  kungfu inspect me.kungfu                 look inside a bundle before sending it
  kungfu import me.kungfu --into ~/Vault   Claude checks each lesson against your notes; pick and load them

export, import and init need the Anthropic API (the SDK, and ANTHROPIC_API_KEY or `ant auth login`).

⚠ kungfu is experimental software: use it at your own risk. init, export and import show a warning
and run only after you type "I accept the risk" (or pass --accept-risk), every time.

Run `kungfu COMMAND --help` for a command's options."""


def _accept_risk_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--accept-risk", action="store_true",
                   help="acknowledge, for this run, that kungfu is experimental and used at your own risk "
                        "(otherwise you're asked to type the phrase)")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="kungfu",
        description="I Know Kung-Fu: package the non-sensitive things you and your AI tools have learned "
                    "(Claude memories, CLAUDE.md and other agent instructions, Obsidian, LLM wikis) and load "
                    "them into someone else's knowledge base.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version",
                    version=f"kungfu {__version__}\nCopyright 2026 Nicole Roy. Licensed under the Apache License 2.0.\n"
                            "Experimental software, provided AS IS, without warranty: use it at your own risk.")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p = sub.add_parser("init", help="find names, orgs, projects and topics in your sources and choose what to scrub")
    p.add_argument("--force", action="store_true", help="redo an existing config, starting from its choices")
    p.add_argument("--defaults", action="store_true", help="don't scan or ask; write a starter config")
    p.add_argument("--accept-all", action="store_true", help="scan and accept every suggestion without asking")
    p.add_argument("--vault", action="append")
    p.add_argument("--wiki", action="append")
    _accept_risk_flag(p)
    p.set_defaults(fn=cmd_init, refused=_init_refused)

    p = sub.add_parser("former-names", help="privately add former names (a deadname, old usernames) to always scrub")
    p.add_argument("--clear", action="store_true", help="remove all of them")
    p.add_argument("--count", action="store_true", help="print how many are saved")
    p.set_defaults(fn=cmd_former_names)

    p = sub.add_parser("locate", help="find git repo folders, Obsidian vaults and LLM wikis under your home and iCloud")
    p.set_defaults(fn=cmd_locate)

    p = sub.add_parser("discover", help="list names, orgs, projects, domains and acronyms found in your sources")
    _source_args(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("scan", help="show what would be offered for export, by topic")
    _source_args(p)
    p.add_argument("--list", action="store_true", help="list every candidate (titles only)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("export", help="choose topics, have Claude write lessons, review them and pack a bundle")
    _source_args(p)
    _topic_args(p)
    p.add_argument("-o", "--output", help="bundle file (default kungfu-<date>.kungfu)")
    p.add_argument("--title")
    p.add_argument("--description")
    p.add_argument("--author", help="name to put in the bundle (omitted unless given)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before sending to Claude; skip the review")
    _accept_risk_flag(p)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("inspect", help="look inside a bundle")
    p.add_argument("bundle")
    p.add_argument("--show", metavar="N|ID", help="print one learning in full")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("rain", help="watch the activity indicator (Ctrl-C to stop)")
    p.add_argument("--seconds", type=float, default=10)
    p.add_argument("--height", type=int, default=None)
    p.add_argument("--message", default="Follow the white rabbit")
    p.set_defaults(fn=cmd_rain)

    p = sub.add_parser("import", help="choose topics, have Claude check each lesson against your notes, and load them")
    p.add_argument("bundle")
    p.add_argument("--obsidian", action="append", metavar="VAULT", help="Obsidian vault; repeatable")
    p.add_argument("--wiki", action="append", metavar="WIKI", help="LLM wiki; repeatable")
    p.add_argument("--into", action="append", metavar="PATH", help="detect vault or wiki; repeatable")
    _topic_args(p)
    p.add_argument("--folder", default="Kung Fu", help="Obsidian folder to import into (default 'Kung Fu')")
    p.add_argument("--direct", action="store_true",
                   help="LLM wiki: also write wiki pages and index entries, not just raw sources")
    p.add_argument("--update", action="store_true", help="overwrite learnings imported before")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before sending to Claude; skip the review")
    p.add_argument("-v", "--verbose", action="store_true")
    _accept_risk_flag(p)
    p.set_defaults(fn=cmd_import)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        ap.print_help()
        return 0
    args = ap.parse_args(argv)
    refused = getattr(args, "refused", None)  # a command that would refuse anyway does so before the warning
    if refused and (code := refused(args)) is not None:
        return code
    if args.cmd in risk.WHAT:
        args.acknowledgement = risk.acknowledge(args.cmd, flag=args.accept_risk, interactive=interactive())
        if args.acknowledgement is None:
            return 1
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        err("\nInterrupted; nothing more written.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
