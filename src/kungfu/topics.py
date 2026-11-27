# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Assign each learning a few topics, so exporters and importers can choose by subject.

Topics come from, in order: tags the source already carries (Obsidian/wiki tags), then a keyword
taxonomy matched against the text. ``kungfu export --distill`` lets Claude replace them.
"""

from __future__ import annotations

import re

from kungfu.model import Learning, slugify

# topic -> keyword pattern. Patterns are matched case-insensitively against title + summary + body.
TAXONOMY: dict[str, str] = {
    "git": r"\bgit\b|\bbranch(es)?\b|\bcommits?\b|\brebase\b|\bmerge[ds]?\b|\bworktrees?\b|\bstash\b",
    "github": r"\bgithub\b|\bgh (pr|issue|api|run)\b|\bpull requests?\b|\bPRs?\b|\bgithub actions\b|\bdependabot\b",
    "ci-cd": r"\bci\b|\bcd\b|\bpipelines?\b|\bdeploy(s|ed|ment|ments)?\b|\brelease[sd]?\b|\bworkflows?\b",
    "testing": r"\btests?\b|\btesting\b|\bpytest\b|\bunittest\b|\bcoverage\b|\bfixtures?\b|\bassert",
    "aws": r"\baws\b|\becs\b|\brds\b|\bs3\b|\blambda\b|\bcloudwatch\b|\biam role|\bsecrets ?manager\b|\becr\b|\bec2\b",
    "infrastructure-as-code": r"\bterraform\b|\bopentofu\b|\btofu\b|\bpulumi\b|\bcloudformation\b|\bstate file\b",
    "containers": r"\bdocker\b|\bcontainers?\b|\bkubernetes\b|\bk8s\b|\bhelm\b|\bimage digests?\b",
    "databases": r"\bsql\b|\bpostgres(ql)?\b|\bmysql\b|\bsqlite\b|\bdatabases?\b|\brds\b|\bschema migrations?\b",
    "security": r"\bsecurity\b|\bsecrets?\b|\bcredentials?\b|\bvulnerab|\bcve\b|\bred[- ]team\b|\bsigning\b|\bthreat\b|\bauthz\b",
    "identity": r"\bsaml\b|\boidc\b|\boauth\b|\bsso\b|\bfederation\b|\bentity ?id\b|\bidp\b|\bmetadata\b|\bmfa\b",
    "python": r"\bpython3?\b|\bpip\b|\bvenv\b|\bpyproject\b|\buv\b|\bpydantic\b",
    "rust": r"\brust\b|\bcargo\b|\bclippy\b|\bcrates?\b",
    "javascript": r"\bjavascript\b|\btypescript\b|\bnode(js)?\b|\bnpm\b|\bpnpm\b|\breact\b",
    "go": r"\bgolang\b|\bgo (build|test|mod)\b",
    "shell": r"\bbash\b|\bzsh\b|\bshell\b|\bsed\b|\bawk\b|\bgrep\b|\bcli\b",
    "claude-code": r"\bclaude code\b|\bclaude\.md\b|\bhooks?\b|\bskills?\b|\bsubagents?\b|\bmcp\b|\bslash commands?\b|\bsettings\.json\b",
    "ai-assistants": r"\bllms?\b|\bprompts?\b|\bagents?\b|\bclaude\b|\bcopilot\b|\bcursor\b|\bmodel\b|\bcontext window\b",
    "knowledge-management": r"\bobsidian\b|\bvault\b|\bwiki\b|\bnotes?\b|\bzettelkasten\b|\bknowledge ?base\b",
    "writing": r"\bwriting\b|\bspelling\b|\btone\b|\bprose\b|\bwording\b|\bstyle guide\b|\bcommit messages?\b",
    "communication": r"\bexplain\b|\bannounce\b|\bask (first|before)\b|\bconfirm\b|\bupdates?\b|\bsummar(y|ize)\b",
    "workflow": r"\bworkflow\b|\bone (branch|pr) at a time\b|\bserial\b|\bparallel\b|\bbatch(ing)?\b|\bfocus\b|\bprioriti",
    "documentation": r"\breadme\b|\badrs?\b|\bdocs?\b|\bdocumentation\b|\bdecision records?\b",
    "architecture": r"\barchitectur(e|al)\b|\bdesign (docs?|decisions?|reviews?)\b|\blayering\b|\bcoupling\b|\babstractions?\b",
    "web": r"\bhtml\b|\bcss\b|\bhttp\b|\brest\b|\bapi\b|\bendpoints?\b|\bcors\b",
    "observability": r"\blogs?\b|\blogging\b|\bmetrics\b|\btracing\b|\bmonitor(ing)?\b|\balerts?\b",
}
_COMPILED = {topic: re.compile(pat, re.I) for topic, pat in TAXONOMY.items()}

# Tags that only mark something for export, not a subject.
META_TAGS = {"kungfu", "share", "shareable", "public", "todo", "draft", "inbox", "wip"}

SOURCE_FALLBACK = {
    "claude-memory": "ai-assistants",
    "claude-md": "ai-assistants",
    "agents-md": "ai-assistants",
    "claude-skill": "claude-code",
    "claude-command": "claude-code",
    "claude-agent": "claude-code",
}


def normalize(topic: str) -> str:
    topic = topic.strip().lstrip("#")
    topic = topic.split("/")[-1] if topic.lower().startswith("kungfu/") else topic.replace("/", "-")
    return slugify(topic, 40)


def _custom_patterns(custom: dict[str, list[str]] | None) -> dict[str, re.Pattern]:
    out = {}
    for name, words in (custom or {}).items():
        words = [w.strip() for w in words if w.strip()]
        if words:
            out[normalize(name)] = re.compile("|".join(r"\b" + re.escape(w) + r"\b" for w in words), re.I)
    return out


def assign(item: Learning, max_topics: int = 3, custom: dict[str, list[str]] | None = None) -> list[str]:
    topics: list[str] = []
    for tag in item.tags:
        t = normalize(tag)
        if t and t not in META_TAGS and t not in topics:
            topics.append(t)
    text = f"{item.title}\n{item.summary}\n{item.body}"
    scores = []
    patterns = {**_COMPILED, **_custom_patterns(custom)}
    for topic, pat in patterns.items():
        hits = len(pat.findall(text))
        # Title hits count triple: they say what the learning is about.
        hits += 2 * len(pat.findall(item.title))
        if hits:
            scores.append((hits, topic))
    scores.sort(key=lambda s: (-s[0], s[1]))
    top = scores[0][0] if scores else 0
    for hits, topic in scores:
        if len(topics) >= max_topics:
            break
        # The strongest match always counts; weaker ones need a few mentions to be more than noise.
        if topics and (hits < 2 or hits < 0.4 * top):
            continue
        if topic not in topics:
            topics.append(topic)
    if not topics:
        topics.append(SOURCE_FALLBACK.get(item.source_type, "general"))
    item.topics = topics[:max_topics] if item.tags == [] else topics[: max(max_topics, len(item.tags))]
    return item.topics


def counts(items: list[Learning]) -> list[tuple[str, int, list[Learning]]]:
    """Topics with the items in each, most common first. An item appears under each of its topics."""
    by: dict[str, list[Learning]] = {}
    for it in items:
        for t in it.topics:
            by.setdefault(t, []).append(it)
    return sorted(((t, len(v), v) for t, v in by.items()), key=lambda x: (-x[1], x[0]))


def select(items: list[Learning], chosen: set[str]) -> list[Learning]:
    """Items with at least one chosen topic."""
    return [it for it in items if chosen.intersection(it.topics)]
