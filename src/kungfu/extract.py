# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Run every source, sanitize, tag with topics and de-duplicate."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from kungfu import topics
from kungfu.config import Config, common_word, org_from_domain
from kungfu.model import Learning
from kungfu.sanitize import Redactor
from kungfu.sources import claude_memory, instructions, llm_wiki, obsidian


@dataclass
class Plan:
    claude: bool = True
    repos: bool = True
    vaults: list[Path] = field(default_factory=list)
    wikis: list[Path] = field(default_factory=list)


@dataclass
class Result:
    items: list[Learning]
    sources: dict[str, int]  # source label -> candidates found
    errors: list[str]
    redactor: Redactor  # reused for anything edited or rewritten after the scan
    hints: list[str] = field(default_factory=list)  # why a source gave little or nothing

    @property
    def offered(self) -> list[Learning]:
        return [i for i in self.items if not i.held]

    @property
    def held(self) -> list[Learning]:
        return [i for i in self.items if i.held]


def project_terms(cfg: Config, plan: Plan) -> dict[str, str]:
    """Repo and project names become "[project]" so learnings don't name internal systems."""
    names: set[str] = set()
    if plan.claude:
        names |= claude_memory.project_names(cfg.claude_dir)
    orgs: set[str] = set()
    local_parts: set[str] = set()
    if plan.repos:
        for repo in instructions.repos_to_read(cfg.repo_roots, cfg.only_my_repos):
            names.add(repo.name)
            owners, domains = instructions.repo_identity(repo)
            orgs |= owners
            orgs |= {o for o in map(org_from_domain, domains) if o}
            local_parts |= {e.split("@")[0] for e in instructions.repo_emails(repo)}
    # Short, generic or ordinary names would redact ordinary words ("docker", "notes", "javascript").
    keep = {n for n in names if (len(n) >= 5 or "-" in n) and n.lower() not in GENERIC_NAMES and not common_word(n)}
    # Distinctive parts of compound names ("acme-zephyrine" -> "zephyrine") are names too, unless
    # they're ordinary or technical words ("metadata-to-yaml" keeps "metadata"; "docker-compose" keeps both).
    for n in list(keep) + [n for n in names if "-" in n or "_" in n]:
        for part in re.split(r"[-_.]", n):
            if len(part) >= 6 and part.lower() not in GENERIC_NAMES and not common_word(part):
                keep.add(part)
    terms = {n: "[project]" for n in keep}
    # Your work address's local part ("jdoe" of jdoe@acme.com) is you.
    for lp in local_parts:
        if len(lp) >= 3:
            terms[lp] = "the user"
    # Code-host owners and work email domains name organizations (or the user's own account).
    user_terms = {k.lower() for k, v in cfg.redact_terms.items() if v == "the user"}
    for org in orgs:
        if len(org) >= 3 and org.lower() not in GENERIC_NAMES and not common_word(org):
            terms[org] = "the user" if org.lower() in user_terms else "the organization"
    return terms


GENERIC_NAMES = {"docs", "notes", "scripts", "dotfiles", "config", "website", "tools", "utils", "examples",
                 "backend", "frontend", "server", "client", "infra", "terraform", "ansible", "playground"}


@dataclass
class Collected:
    items: list[Learning]  # raw, unredacted
    sources: dict[str, int]
    errors: list[str]
    people: set[str]  # names from People folders and person pages
    hints: list[str] = field(default_factory=list)


def collect(cfg: Config, plan: Plan) -> Collected:
    """Read every source. Nothing is redacted yet; callers must not let this text leave the machine."""
    raw: list[Learning] = []
    sources: dict[str, int] = {}
    errors: list[str] = []
    people: set[str] = set()
    wikis = list(plan.wikis)

    def take(label: str, gen) -> None:
        try:
            batch = list(gen)
        except Exception as exc:  # one broken source shouldn't stop the export
            errors.append(f"{label}: {exc}")
            return
        sources[label] = sources.get(label, 0) + len(batch)
        raw.extend(batch)

    if plan.claude:
        take("Claude Code memories", claude_memory.scan(cfg.claude_dir))
    if plan.claude or plan.repos:
        take(
            "Agent instruction files (CLAUDE.md, AGENTS.md, rules, skills)",
            instructions.scan(cfg.claude_dir if plan.claude else None, cfg.repo_roots if plan.repos else [],
                              cfg.only_my_repos),
        )
    vaults = list(plan.vaults)
    hints: list[str] = []
    for wiki in list(wikis):
        if wiki.is_dir() and not llm_wiki.is_wiki(wiki) and obsidian.is_vault(wiki):
            wikis.remove(wiki)
            vaults.append(wiki)
            hints.append(f"{wiki} is an Obsidian vault, not an LLM wiki (no wiki/ + index.md/log.md); "
                         "read it as a vault. Move it to obsidian_vaults in the config.")
    for vault in vaults:
        if llm_wiki.is_wiki(vault) and not obsidian.is_vault(vault):
            wikis.append(vault)
            continue
        if not vault.is_dir():
            errors.append(f"Obsidian vault not found: {vault}")
            continue
        people |= obsidian.people_names(vault, cfg.people_folders)
        stats: dict = {}
        take(f"Obsidian: {vault.name}",
             obsidian.scan(vault, cfg.include_folders, cfg.deny_folders, cfg.share_tags, stats=stats))
        if stats.get("notes") and not stats.get("offered"):
            hints.append(
                f"Obsidian: {vault.name} has {stats['notes']} notes but none are opted in "
                f"({stats['denied']} in private folders such as People or Daily, {stats['untagged']} untagged). "
                f"Tag notes #kungfu, add share: true, or list folders under include_folders "
                f"(`kungfu init --force`, or `--include-folder NAME`).")
    for wiki in wikis:
        if not wiki.is_dir():
            errors.append(f"LLM wiki not found: {wiki}")
            continue
        people |= llm_wiki.people_names(wiki)
        take(f"LLM wiki: {wiki.name}", llm_wiki.scan(wiki))
    for e in errors:
        if "not found" in e and "\\" in e:
            hints.append("A path above contains a backslash; write paths without shell escaping "
                         "(Mobile Documents, not Mobile\\ Documents).")
            break
    return Collected(items=raw, sources=sources, errors=errors, people=people, hints=hints)


def build_redactor(cfg: Config, plan: Plan, people: set[str]) -> Redactor:
    keep = {k.lower() for k in cfg.keep_terms}
    terms = {k: v for k, v in {**project_terms(cfg, plan), **cfg.redact_terms}.items() if k.lower() not in keep}
    former = cfg.former
    # People-folder notes named with a former name are you, not someone else.
    # A People-folder note titled with an ordinary word ("person", "template") is a template, not a name.
    people = {p for p in people if p.lower() not in keep and not (former and former.matches(p))
              and not (" " not in p.strip() and common_word(p))}
    cased = {k: v for k, v in getattr(cfg, "cased_terms", {}).items() if k.lower() not in keep}
    return Redactor(terms=terms, people=people, allow_domains=cfg.allow_domains, former=former, cased_terms=cased)


def run(cfg: Config, plan: Plan) -> Result:
    got = collect(cfg, plan)
    redactor = build_redactor(cfg, plan, got.people)
    excluded = {topics.normalize(t) for t in cfg.exclude_topics}
    seen: dict[str, Learning] = {}
    for item in got.items:
        redactor.apply(item)
        item.tags = redactor.clean_tags(item.tags)
        topics.assign(item, custom=cfg.custom_topics)
        if excluded and item.topics:
            remaining = [t for t in item.topics if t not in excluded]
            if not remaining:
                item.held = item.held or f"only in topics you excluded ({', '.join(item.topics)})"
            else:
                item.topics = remaining
        if item.id in seen:
            continue
        seen[item.id] = item
    return Result(items=list(seen.values()), sources=got.sources, errors=got.errors, redactor=redactor,
                  hints=got.hints)
