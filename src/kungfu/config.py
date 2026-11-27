# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""User configuration: where to look, and what to redact.

Lives at ``~/.config/kungfu/config.toml`` (override with ``KUNGFU_CONFIG``). Everything is optional.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REPO_ROOTS = ["~/Documents/git", "~/src", "~/code", "~/git", "~/projects", "~/Developer"]

# Folders that hold notes about people, days and meetings. Never exported unless explicitly included.
DEFAULT_DENY_FOLDERS = [
    "People", "Persons", "Person", "Contacts", "Daily", "Daily Notes", "Journal", "Journals",
    "Meetings", "Meeting Notes", "Briefings", "Inbox", "Private", "Personal", "Archive", "Weekly Status",
    "_attachments", "attachments", "Attachments", "_templates", "Templates", "_system", ".trash",
]

# Folder names whose note titles are people's names; used to redact those names everywhere.
PEOPLE_FOLDERS = ["People", "Persons", "Person", "Contacts", "Team", "Staff"]

DEFAULT_ALLOW_DOMAINS = [
    "github.com", "gitlab.com", "docs.python.org", "python.org", "developer.mozilla.org", "wikipedia.org",
    "anthropic.com", "claude.com", "claude.ai", "opentofu.org", "registry.opentofu.org", "obsidian.md",
    "rust-lang.org", "docs.rs", "crates.io", "pypi.org", "npmjs.com", "nodejs.org", "go.dev",
    "aws.amazon.com", "docs.aws.amazon.com", "cloud.google.com", "learn.microsoft.com",
    "stackoverflow.com", "kubernetes.io", "docker.com", "postgresql.org", "ietf.org", "rfc-editor.org",
]


def config_path() -> Path:
    return Path(os.environ.get("KUNGFU_CONFIG", "~/.config/kungfu/config.toml")).expanduser()


@dataclass
class Config:
    claude_dir: Path = field(default_factory=lambda: Path("~/.claude").expanduser())
    repo_roots: list[Path] = field(default_factory=list)
    vaults: list[Path] = field(default_factory=list)
    wikis: list[Path] = field(default_factory=list)
    include_folders: list[str] = field(default_factory=list)
    deny_folders: list[str] = field(default_factory=lambda: list(DEFAULT_DENY_FOLDERS))
    people_folders: list[str] = field(default_factory=lambda: list(PEOPLE_FOLDERS))
    share_tags: list[str] = field(default_factory=lambda: ["kungfu", "share", "shareable"])
    # term -> replacement. Matched case-insensitively on word boundaries.
    redact_terms: dict[str, str] = field(default_factory=dict)
    allow_domains: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOW_DOMAINS))
    # Like redact_terms, but matched only when capitalized exactly as written ("Smith", not "smith").
    cased_terms: dict[str, str] = field(default_factory=dict)
    # Terms never redacted, even when detected automatically (repo names, People notes, git owners).
    keep_terms: list[str] = field(default_factory=list)
    # Topics never offered for export.
    exclude_topics: list[str] = field(default_factory=list)
    # Your own topics: name -> keywords that put a learning under it.
    custom_topics: dict[str, list[str]] = field(default_factory=dict)
    # Only read repos you have committed to (or that belong to an owner of one you have).
    only_my_repos: bool = True
    # Former names (kungfu.former_names.FormerNames), kept hashed in their own file.
    former: object = None
    distill_model: str = "claude-opus-5-5"


def clean_path(raw: str) -> str:
    """Undo shell quoting people paste into prompts and configs: 'My\\ Vault', "My Vault", ~."""
    import shlex

    s = str(raw).strip()
    if not s:
        return s
    if "\\" in s or s[0] in "\"'":
        try:
            s = " ".join(shlex.split(s))
        except ValueError:
            s = s.strip("\"'")
    return str(Path(s).expanduser())


def _paths(values) -> list[Path]:
    return [Path(clean_path(v)) for v in (values or []) if str(v).strip()]


def known_vaults() -> list[Path]:
    """Obsidian vaults on this machine: Obsidian's own registry, plus vaults nested one level inside
    those and in common folders. Read locally; nothing is opened in Obsidian."""
    import json

    found: list[Path] = []
    registries = [Path("~/Library/Application Support/obsidian/obsidian.json"), Path("~/.config/obsidian/obsidian.json"),
                  Path("~/AppData/Roaming/obsidian/obsidian.json")]
    for reg in registries:
        try:
            data = json.loads(reg.expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found += [Path(v["path"]) for v in data.get("vaults", {}).values() if isinstance(v, dict) and v.get("path")]
    bases = found + [Path("~/Library/Mobile Documents/iCloud~md~obsidian/Documents").expanduser(),
                     Path("~/Documents").expanduser(), Path("~/Obsidian").expanduser()]
    for base in bases:
        try:
            found += [d for d in base.iterdir() if d.is_dir() and (d / ".obsidian").is_dir()]
        except OSError:
            continue
    out: list[Path] = []
    for p in found:
        if (p / ".obsidian").is_dir() and p not in out:
            out.append(p)
    return out


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    cfg = Config()
    raw: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    src = raw.get("sources", {})
    if "claude_dir" in src:
        cfg.claude_dir = Path(src["claude_dir"]).expanduser()
    cfg.repo_roots = _paths(src.get("repo_roots")) or [p for p in _paths(DEFAULT_REPO_ROOTS) if p.is_dir()]
    cfg.vaults = _paths(src.get("obsidian_vaults"))
    cfg.wikis = _paths(src.get("llm_wikis"))
    cfg.only_my_repos = bool(src.get("only_my_repos", True))
    obs = raw.get("obsidian", {})
    cfg.include_folders = list(obs.get("include_folders", []))
    cfg.deny_folders = list(obs.get("deny_folders", cfg.deny_folders))
    cfg.people_folders = list(obs.get("people_folders", cfg.people_folders))
    cfg.share_tags = list(obs.get("share_tags", cfg.share_tags))
    red = raw.get("redact", {})
    auto = auto_redact_terms()
    configured = {str(k): str(v) for k, v in red.get("terms", {}).items()}
    # Your own name and usernames are "the user", even if an older config filed one as a [person].
    auto_l = {k.lower() for k in auto}
    configured = {k: v for k, v in configured.items() if not (k.lower().lstrip("@") in auto_l and v == "[person]")}
    cfg.redact_terms = {**auto, **configured}
    cfg.allow_domains = cfg.allow_domains + list(red.get("allow_domains", []))
    cfg.keep_terms = [str(k) for k in red.get("keep", [])]
    cfg.cased_terms = {str(k): str(v) for k, v in red.get("cased_terms", {}).items()}
    keep = {k.lower() for k in cfg.keep_terms}
    cfg.redact_terms = {k: v for k, v in cfg.redact_terms.items() if k.lower() not in keep}
    top = raw.get("topics", {})
    cfg.exclude_topics = [str(t) for t in top.get("exclude", [])]
    cfg.custom_topics = {str(k): [str(x) for x in (v if isinstance(v, list) else [v])]
                         for k, v in top.get("custom", {}).items()}
    cfg.distill_model = raw.get("distill", {}).get("model", cfg.distill_model)
    from kungfu import former_names

    cfg.former = former_names.FormerNames.load(path.parent / "former-names.json"
                                               if "KUNGFU_FORMER_NAMES" not in os.environ else None)
    return cfg


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def github_handles() -> set[str]:
    """Your code-host usernames, read locally from the GitHub CLI's hosts.yml (no network)."""
    handles: set[str] = set()
    path = Path(os.environ.get("GH_CONFIG_DIR", "~/.config/gh")).expanduser() / "hosts.yml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    in_users = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("user:"):
            handles.add(stripped.split(":", 1)[1].strip())
            in_users = False
        elif stripped == "users:":
            in_users = True
        elif in_users and stripped.endswith(":") and line.startswith(" " * 8):
            handles.add(stripped[:-1])
        elif in_users and not line.startswith(" " * 8):
            in_users = False
    gh_user = _run(["git", "config", "--global", "github.user"])
    if gh_user:
        handles.add(gh_user)
    return {h for h in handles if h and len(h) >= 2}


def name_variants(full_name: str) -> set[str]:
    """Usernames people derive from their name: 'Jane Doe' -> jdoe, janedoe, jane.doe, jane_doe, doej, doe.jane."""
    parts = [p.lower() for p in full_name.replace(".", " ").split() if p.isalpha()]
    if len(parts) < 2:
        return set()
    first, last = parts[0], parts[-1]
    return {f"{first[0]}{last}", f"{first}{last}", f"{first}.{last}", f"{first}_{last}", f"{first}-{last}",
            f"{last}{first[0]}", f"{last}.{first}", f"{first}{last[0]}"}


def _own_names() -> set[str]:
    names = {_run(["git", "config", "--global", "user.name"])}
    if os.uname().sysname == "Darwin":
        names.add(_run(["id", "-F"]))
    return {n for n in names if n}


def user_identities() -> set[str]:
    """Lowercased names, emails and handles that mean "you" in git history."""
    ids = {_run(["git", "config", "--global", "user.name"]), _run(["git", "config", "--global", "user.email"])}
    if os.uname().sysname == "Darwin":
        ids.add(_run(["id", "-F"]))
    ids |= github_handles()
    for name in _own_names():
        ids |= name_variants(name)
    return {i.lower() for i in ids if i}


def auto_redact_terms() -> dict[str, str]:
    """The exporter's own name and login, from git config and the OS. These become 'the user'."""
    terms: dict[str, str] = {}
    names = {_run(["git", "config", "--global", "user.name"])}
    if os.uname().sysname == "Darwin":
        names.add(_run(["id", "-F"]))
    for name in filter(None, names):
        terms[name] = "the user"
        for part in name.split():
            if len(part) >= 3:
                terms.setdefault(part, "the user")
    email = _run(["git", "config", "--global", "user.email"])
    if "@" in email:
        local, _, domain = email.partition("@")
        if len(local) >= 3:
            terms.setdefault(local, "the user")
        org = org_from_domain(domain)
        if org:
            terms.setdefault(org, "the organization")
    user = os.environ.get("USER", "")
    if len(user) >= 3:
        terms.setdefault(user, "the user")
    for handle in github_handles():
        if len(handle) >= 3:
            terms.setdefault(handle, "the user")
    for name in _own_names():
        for v in name_variants(name):
            if len(v) >= 4:
                terms.setdefault(v, "the user")
    return terms


PUBLIC_MAIL = {"gmail", "googlemail", "icloud", "me", "mac", "outlook", "hotmail", "live", "msn", "yahoo",
               "proton", "protonmail", "pm", "fastmail", "aol", "gmx", "zoho", "hey", "duck", "noreply", "users"}


def org_from_domain(domain: str) -> str | None:
    """'jdoe@eng.acme.co.uk' -> 'acme'. None for personal mail providers."""
    labels = [x for x in domain.lower().split(".") if x]
    if len(labels) < 2 or "github" in labels:
        return None
    # Skip two-letter second-level labels such as co.uk / ac.jp.
    org = labels[-3] if len(labels) >= 3 and len(labels[-2]) <= 3 and len(labels[-1]) == 2 else labels[-2]
    if org in PUBLIC_MAIL or org in ("example", "test", "invalid", "localhost", "local") or len(org) < 3:
        return None
    return org


_DICT_CACHE: dict[str, set[str]] = {}


def _wordlist(name: str, paths: tuple[str, ...], lowercase_only: bool) -> set[str]:
    if name not in _DICT_CACHE:
        words: set[str] = set()
        for path in paths:
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    for w in fh:
                        w = w.strip()
                        if w and (not lowercase_only or w.islower()):
                            words.add(w.lower())
                break
            except OSError:
                continue
        _DICT_CACHE[name] = words
    return _DICT_CACHE[name]


def dictionary_words() -> set[str]:
    """Ordinary (lowercase) English words. Capitalized entries in the system list are proper nouns."""
    return _wordlist("words", ("/usr/share/dict/words", "/usr/dict/words"), lowercase_only=True)


COMMON_TECH_WORDS = {
    "metadata", "schema", "schemas", "json", "yaml", "toml", "xml", "api", "apis", "sdk", "cli", "docker", "dockerfile",
    "container", "containers", "kubernetes", "helm", "javascript", "typescript", "python", "golang", "rust", "java",
    "youtube", "github", "gitlab", "examples", "example", "tools", "utils", "test", "tests", "demo", "demos",
    "sandbox", "scripts", "config", "configs", "infra", "terraform", "opentofu", "ansible", "website", "backend",
    "frontend", "server", "client", "proxy", "gateway", "service", "services", "worker", "workers", "lambda",
    "function", "functions", "plugin", "plugins", "theme", "themes", "template", "templates", "starter", "boilerplate",
    "notes", "docs", "dotfiles", "playground", "workshop", "tutorial", "homework", "exercises", "generator",
    "converter", "parser", "validator", "exporter", "importer", "wrapper", "helper", "helpers", "library", "core",
    "common", "shared", "legacy", "archive", "backup", "backups", "mirror", "fork", "upstream", "saml", "oidc",
    "oauth", "ldap", "sso", "idp", "mfa", "web", "app", "apps", "site", "data", "db", "sql", "query", "queries",
    # Modern compounds the 1934 system dictionary lacks.
    "baseline", "baselines", "workflow", "workflows", "snapshot", "snapshots", "dataset", "datasets", "pipeline",
    "pipelines", "changelog", "codebase", "database", "databases", "datastore", "dashboard", "dashboards",
    "endpoint", "endpoints", "backlog", "roadmap", "toolkit", "toolchain", "framework", "frameworks", "runtime",
    "namespace", "namespaces", "webhook", "webhooks", "username", "password", "passwords", "keyboard", "desktop",
    "laptop", "smartphone", "website", "websites", "online", "offline", "inbox", "spreadsheet", "timeline",
    "playbook", "runbook", "runbooks", "checklist", "checklists", "readme", "todo", "todos", "markdown",
}


def common_word(word: str) -> bool:
    """An ordinary English or technical word ("metadata", "docker", "flattened"): never a project or person name
    on its own. Compound and CamelCase names ("DocuFlow", "acme-zephyrine") are not common words."""
    w = word.lower().strip()
    if not w or not w.isalpha():
        return False
    words = dictionary_words()
    return (w in COMMON_TECH_WORDS or w in words
            or any(w.endswith(suf) and (w[: -len(suf)] in words or w[: -len(suf)] + "e" in words)
                   for suf in ("s", "es", "ed", "d", "ing", "ly", "er", "ers")))


def first_names() -> set[str]:
    """Common given names, lowercased: the list bundled with kungfu (many cultures) plus the
    system's propernames list where there is one."""
    if "given" not in _DICT_CACHE:
        bundled: set[str] = set()
        try:
            text = (Path(__file__).parent / "data" / "given_names.txt").read_text(encoding="utf-8")
            for line in text.splitlines():
                if not line.startswith("#"):
                    bundled.update(w for w in line.split() if w.isalpha())
        except OSError:
            pass
        system = _wordlist("propernames", ("/usr/share/dict/propernames",), lowercase_only=False)
        # Names that are much more often products or abbreviations in technical notes.
        not_names = {"rex", "mac", "nan", "linux", "ruby", "julia", "pip", "ada"}
        _DICT_CACHE["given"] = (bundled | system | FALLBACK_FIRST_NAMES) - not_names
    return _DICT_CACHE["given"]


FALLBACK_FIRST_NAMES = {
    "james", "john", "robert", "michael", "william", "david", "richard", "joseph", "thomas", "charles", "mary",
    "patricia", "jennifer", "linda", "elizabeth", "barbara", "susan", "jessica", "sarah", "karen", "nancy",
    "lisa", "betty", "margaret", "sandra", "ashley", "emily", "michelle", "daniel", "matthew", "anthony", "mark",
    "paul", "steven", "andrew", "kenneth", "joshua", "kevin", "brian", "george", "edward", "ronald", "timothy",
    "jason", "jeffrey", "ryan", "jacob", "gary", "nicholas", "eric", "jonathan", "stephen", "larry", "justin",
    "scott", "brandon", "benjamin", "samuel", "frank", "gregory", "raymond", "alexander", "patrick", "jack",
    "dennis", "jerry", "tyler", "aaron", "jose", "adam", "henry", "nathan", "douglas", "zachary", "peter", "kyle",
    "walter", "ethan", "jeremy", "harold", "keith", "christian", "roger", "noah", "gerald", "carl", "terry",
    "sean", "austin", "arthur", "lawrence", "jesse", "dylan", "bryan", "joe", "jordan", "billy", "bruce",
    "albert", "willie", "gabriel", "logan", "alan", "juan", "wayne", "roy", "ralph", "randy", "eugene", "vincent",
    "russell", "elijah", "louis", "bobby", "philip", "johnny", "jane", "anna", "emma", "olivia", "sophia", "maria",
    "laura", "julie", "amy", "angela", "helen", "rachel", "carol", "ruth", "sharon", "kim", "deborah", "rebecca",
    "nicole", "christine", "katherine", "catherine", "heather", "diane", "julia", "victoria", "kelly", "joan",
    "evelyn", "lauren", "judith", "megan", "cheryl", "andrea", "hannah", "martha", "jacqueline", "frances",
    "gloria", "ann", "teresa", "kathryn", "sara", "janet", "alice", "madison", "doris", "abigail", "denise",
    "amber", "marie", "danielle", "brittany", "diana", "natalie", "sophie", "alexis", "lori", "kayla", "grace",
}


TEMPLATE = """\
# I Know Kung-Fu configuration. Every key is optional. Written by `kungfu init`; edit freely.

[sources]
# claude_dir = "~/.claude"
# Folders that contain your git repositories (searched a few levels deep for CLAUDE.md, AGENTS.md, etc.)
repo_roots = {repo_roots}
# Only read repos you have committed to (or that share an owner with one). false = read every repo.
only_my_repos = {only_my_repos}
# Obsidian vaults to export from. Only notes tagged #kungfu (or share: true), or in include_folders, are offered.
obsidian_vaults = {vaults}
# LLM wikis (Karpathy-style: raw/ + wiki/ + index.md + log.md) to export from.
llm_wikis = {wikis}

[obsidian]
# Folders whose notes are offered for export even without a #kungfu tag.
include_folders = {include_folders}
# Folders never exported (people, daily notes, meetings...). Uncomment to replace the built-in list.
# deny_folders = {deny_folders}

[redact]
# Never redact these, even when they're detected automatically (repo names, People notes, git owners).
keep = {keep}

[redact.terms]
# Replaced (case-insensitively, as whole words) in everything exported. Your own name, login and
# GitHub handle, your repos' names and owners, and names of notes in your People folder are
# scrubbed automatically on top of these.
{terms}

[redact.cased_terms]
# Names that are also ordinary words ("Smith", "Young", "Rose"): replaced only when capitalized as
# written here, so "smith" and "young" in ordinary sentences are left alone.
{cased}

[topics]
# Topics never offered for export.
exclude = {exclude}

[topics.custom]
# Your own topics: name = [keywords that put a learning under it].
{custom}

[distill]
# Model used by `kungfu export --distill`.
model = "claude-opus-5-5"
"""

REPLACEMENT_GROUPS = [
    ("the user", "You"),
    ("[person]", "People"),
    ("the organization", "Organizations"),
    ("[project]", "Projects, products and codenames"),
    ("[domain]", "Domains"),
    ("[acronym]", "Acronyms"),
    ("[name]", "Other names"),
]


def toml_list(values) -> str:
    import json

    return "[" + ", ".join(json.dumps(str(v)) for v in values) + "]"


def render_template(repo_roots, vaults, wikis, include_folders, terms: dict[str, str], keep=(), exclude=(),
                    custom: dict[str, list[str]] | None = None, only_my_repos: bool = True,
                    cased: dict[str, str] | None = None) -> str:
    import json

    groups: dict[str, list[str]] = {}
    for term, repl in sorted(terms.items(), key=lambda kv: kv[0].lower()):
        groups.setdefault(repl, []).append(f"{json.dumps(term)} = {json.dumps(repl)}")
    lines: list[str] = []
    known = [r for r, _ in REPLACEMENT_GROUPS]
    for repl, label in REPLACEMENT_GROUPS + [(r, f"Replaced with {r}") for r in groups if r not in known]:
        if repl in groups:
            lines += [f"# {label}", *groups[repl], ""]
    term_lines = "\n".join(lines).rstrip() or '# "Acme Corp" = "the organization"\n# "Jane Doe" = "[person]"'
    custom_lines = "\n".join(f"{json.dumps(k)} = {toml_list(v)}" for k, v in (custom or {}).items()) or (
        '# "federation" = ["saml", "metadata", "entity id"]')
    return TEMPLATE.format(
        repo_roots=toml_list(repo_roots),
        only_my_repos="true" if only_my_repos else "false",
        vaults=toml_list(vaults),
        wikis=toml_list(wikis),
        include_folders=toml_list(include_folders),
        deny_folders=toml_list(DEFAULT_DENY_FOLDERS),
        keep=toml_list(keep),
        terms=term_lines,
        exclude=toml_list(exclude),
        custom=custom_lines,
        cased="\n".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in sorted((cased or {}).items()))
        or '# "Smith" = "[person]"',
    )
