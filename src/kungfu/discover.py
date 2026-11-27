# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Find the names, organizations, projects, domains and acronyms in your sources that you'd
probably want scrubbed, so ``kungfu init`` can offer them as a curated list.

Everything is read locally from the same text an export would draw on (including held-back
items), plus a few places that name people and organizations directly:

  people          People-folder notes, wiki person pages, git commit authors, "First Last" in the
                  text (first name from the system's given-names list), first.last@ addresses, @handles
  organizations   git remote owners, work email domains, "Acme Inc" / "University of X" phrases
  projects        repo and Claude-project names, CamelCase names (DocuFlow), capitalized words
                  that never appear in lowercase and aren't dictionary words or tech terms
  domains         non-public domains in URLs and email addresses
  acronyms        all-caps words used 3+ times that aren't common tech acronyms (often teams)

Nothing here leaves the machine.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from kungfu import config as config_mod
from kungfu.config import PUBLIC_MAIL, Config, dictionary_words, first_names, org_from_domain
from kungfu.extract import GENERIC_NAMES, Plan, collect
from kungfu.sources import claude_memory, instructions

REPLACEMENT = {
    "person": "[person]",
    "organization": "the organization",
    "project": "[project]",
    "domain": "[domain]",
    "acronym": "[acronym]",
    "other": "[name]",
}
CATEGORY_TITLES = {
    "person": "People",
    "organization": "Organizations",
    "project": "Projects, products and codenames",
    "domain": "Domains",
    "acronym": "Acronyms (often teams, programs or internal systems)",
    "other": "Other capitalized names (people, places or products?)",
}
# Acronyms are the noisiest category, so they start unselected.
DEFAULT_SELECTED = {"person": True, "organization": True, "project": True, "domain": True, "acronym": False,
                    "other": False}
# People are never capped: a name left off the list is a name left in the text.
LIMITS = {"person": None, "organization": 30, "project": 60, "domain": 30, "acronym": 30, "other": 80}

TECH = set("""
github gitlab bitbucket git python rust cargo docker kubernetes helm terraform opentofu tofu postgres postgresql
mysql sqlite redis nginx apache linux macos ios ipados android windows ubuntu debian homebrew brew aws gcp azure
ecs eks ecr rds ec2 iam s3 lambda cloudwatch cloudfront cloudformation route53 ssm kms sns sqs sts arn alb elb
nlb acm waf vpc oauth oidc saml jwt json yaml toml html css http https api apis rest graphql grpc sql cli ci cd
pr prs ssh tls ssl dns url urls uri uuid utf ascii markdown obsidian claude anthropic openai chatgpt gpt copilot
cursor vscode vim neovim emacs zsh bash fish npm pnpm yarn node nodejs javascript typescript react vue svelte
django flask fastapi rails ruby golang java kotlin swift xcode pytest unittest mermaid adr adrs readme todo
fixme mcp llm llms ai ide os cpu gpu ram ssd pdf csv xml svg png jpeg jpg gif mfa sso idp sp ldap scim rbac
cve cves owasp nist iso rfc rfcs ietf w3c iana utc gmt est pst edt pdt usd faq kpi okr roi mvp poc eta tbd
asap fyi wip ui ux qa sla slo sre devops gitops iac ip ipv4 ipv6 tcp udp smtp imap sdk sdks cdk ami eni
pypi crates npmjs slack zoom teams outlook office microsoft google apple amazon gmail icloud safari chrome
firefox webkit chromium electron tauri wasm webassembly opentelemetry prometheus grafana datadog sentry
pagerduty jira confluence notion figma miro trello asana linear dependabot renovate actions workflows
mac pc usb hdmi wifi bluetooth vpn lan wan md env dotenv makefile dockerfile ok id ids tl dr eg ie etc vs
pm am ceo cto cio ciso vp avp svp evp hr it ops dev prod staging qa uat pdt mst cst cdt bst cet
tsv tsx jsx yml ini cfg log logs tmp var usr etc bin src lib pkg cmd fmt str int bool err ctx
qemu posix abnf bnf ebnf rpc idl dco gnu gpl lgpl mit bsd eof crlf tty pty ansi unicode regex fifo lifo dag fsm
ast ieee ecma iso8601 tls13 hsm tpm fido webauthn totp hotp pki csr crl ocsp x509 pem der asn ber pkcs rsa ecdsa
uefi ipc spdx jwks jwk jws jwe mpl dsa smt tla tla+ acvp bios acpi psci mame arm x86 amd64 arm64 riscv simd
powershell airplay chacha20 licenseref rustcrypto haiku sel4 entityid idps orgs sps macbook iphone ipad
youtube linkedin openssl openssh libressl boringssl webpki rustls tokio serde axum hyper actix wireguard
ed25519 sha md5 hmac aes gcm cbc kdf otp nat cidr bgp arp icmp mtu ttl cdn cors csrf xss csp hsts ssrf rce dos
ddos siem soc edr xdr cli tui gui wysiwyg crud orm dsl repl lsp rfc8259 http2 http3 quic ascii utf8 utf16 eol
""".split())

UPPER_WORDS = set("""
the and not never always must should important critical note notes todo warning warn yes no all any none
do don't dont use only new old fix bug see why how what when where who this that these those for with from
into before after every each run read write check add remove keep stop start end error errors fail failed
pass passed ok true false null nan done wip draft final tldr faq tbd fyi asap nb ps re fwd
""".split())

COMMON_TLDS = set("""
com org net edu gov mil int io dev ai app co us uk ca de fr au nz jp cn in eu ch nl se no dk fi es it be at
ie info biz cloud tech me tv xyz site online systems network email
""".split())
PUBLIC_DOMAINS = {"example.com", "example.org", "example.net", "localhost", "google.com", "youtube.com",
                  "doi.org", "w3.org", "12factor.net", "schema.org", "json-schema.org", "semver.org", "spdx.org",
                  "letsencrypt.org", "cloudflare.com", "apple.com", "mozilla.org", "gnu.org", "kernel.org",
                  "slack.com", "zoom.us", "microsoft.com", "office.com", "outlook.com", "live.com", "atlassian.com",
                  "salesforce.com", "okta.com", "amazon.com", "amazonaws.com", "github.io", "githubusercontent.com",
                  "gitlab.com", "notion.so", "dropbox.com", "box.com", "figma.com", "openai.com", "anthropic.com",
                  "twitter.com", "x.com", "linkedin.com", "facebook.com", "medium.com", "substack.com"}

NAME_PAIR = re.compile(r"\b([A-Z][a-z]{1,15})[ \t]+([A-Z][a-z'\-]{1,20}(?:-[A-Z][a-z]+)?)\b")
FIRST_LAST_EMAIL = re.compile(r"\b([a-z]{2,})\.([a-z]{2,})@([a-z0-9.\-]+\.[a-z]{2,})\b", re.I)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
HANDLE = re.compile(r"(?<![\w.@/`])@([a-z][a-z0-9._\-]{2,30})(?![\w/@])", re.I)
ORG_SUFFIX = re.compile(
    r"\b((?:[A-Z][\w&'.\-]*\s+){0,3}[A-Z][\w&'.\-]*)\s+(Inc|Corp|Corporation|LLC|Ltd|GmbH|University|College|"
    r"Foundation|Institute|Consortium|Association|Labs|Laboratory|Agency|Council|Alliance|Society)\b\.?"
)
UNIVERSITY_OF = re.compile(r"\b(University of (?:[A-Z][a-z]+)(?:\s+[A-Z][a-z]+){0,2})")
CAMEL = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+|[a-z]+[A-Z][A-Za-z0-9]+)\b")
CODE_SUFFIX = re.compile(r"(Controller|Service|Handler|Factory|Provider|Exception|Error|Config|Test|Tests|Spec|"
                         r"Request|Response|Builder|Helper|Util|Utils|Impl|Interface|Type|Model|View|Component|"
                         r"Person|Entry|Item|List|Map|Set|Id|ID|Ids|Url|URL)$")
CAPWORD = re.compile(r"(?<!['’])\b([A-Z][a-z]{3,})\b")
# Contexts only people appear in. Prepositions like "from" or "with" are left out on purpose:
# products appear after them too ("from Acme").
PERSON_BEFORE = re.compile(
    r"\b(?i:ask|asked|asking|told|tell|thanks|thank you|thank|cc|cc'd|ping|pinged|emailed|messaged|met|"
    r"talked to|spoke with|spoke to|meeting with|1:1 with)[ \t]+([A-Z][\w'’\-]{2,})")
PERSON_AFTER = re.compile(
    r"\b([A-Z][\w'’\-]{2,})\s+(?:said|says|asked|asks|wrote|writes|mentioned|thinks|suggested|noted|replied|"
    r"agreed|explained|confirmed|emailed|is out|was out|will send|sent|owns|leads|reports to)\b")
ACRONYM = re.compile(r"(?<![\w$])([A-Z][A-Z0-9&+]{2,7})(?![\w])")
URL_HOST = re.compile(r"\bhttps?://(?:[^@/\s]+@)?([A-Za-z0-9.\-]+\.[A-Za-z]{2,12})", re.I)
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)

# LinkedIn, GitHub, OpenTofu: internal capitals mark a product or organization, not a person, unless the
# word follows a surname pattern (McKay, MacLeod, DeShawn, LaToya, VanDyke, O'Brien).
NAME_CAMEL_OK = re.compile(r"^(?:Mc|Mac|De|Di|Da|La|Le|Van|Von|O['’]|D['’])[A-Z][a-z]+$")


def _camel(word: str) -> bool:
    return bool(re.search(r"[a-z][A-Z]", word)) and not NAME_CAMEL_OK.match(word)


# Pronoun contractions look capitalized and sit before "said" or "asked": not names.
CONTRACTION = re.compile(r"^(?:I|We|You|They|He|She|It|There|That|What|Who|Here|Let)['’]", re.I)
# Slack/Teams broadcast mentions, placeholders and well-known bots: not people.
NOT_HANDLES = {
    "channel", "here", "everyone", "all", "mention", "mentions", "user", "username", "someone", "somebody", "me",
    "you", "team", "group", "admin", "admins", "example", "handle", "name", "owner", "owners", "reviewer",
    "reviewers", "assignee", "author", "claude", "copilot", "dependabot", "renovate", "github-actions", "codecov",
    "sonarcloud", "snyk", "mergify", "kodiakhq", "netlify", "vercel", "types", "angular", "babel", "vue", "react",
}

CALENDAR = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "today", "tomorrow",
}

NOT_SURNAME = {
    "the", "and", "for", "with", "from", "into", "code", "desktop", "pages", "actions", "workflow", "workflows",
    "notes", "note", "team", "teams", "group", "office", "project", "projects", "server", "client", "service",
    "services", "api", "app", "apps", "docs", "guide", "manual", "board", "council", "committee", "meeting",
    "meetings", "review", "reviews", "plan", "update", "updates", "brief", "briefing", "report", "summary",
    "university", "college", "institute", "foundation", "labs", "inc", "corp", "says", "said", "asked", "wrote",
    "org", "orgs", "metadata", "chart", "charts", "directory", "federation", "policy", "program", "programs",
    "platform", "portal", "registry", "consortium", "steering", "community", "network", "cloud", "lab", "working",
    "operations", "ops", "support", "engineering", "security", "identity", "trust", "services", "data", "systems",
}


@dataclass
class Candidate:
    term: str
    category: str
    count: int = 0  # mentions in the text an export would draw on
    evidence: set[str] = field(default_factory=set)
    auto: bool = False  # already scrubbed automatically (repo names, People notes, git owners...)
    cased: bool = False  # also an ordinary word ("Smith", "Young"): scrub only when capitalized

    @property
    def suggested(self) -> bool:
        """Preselected in the curated list. Weak text-only finds start unselected."""
        if not DEFAULT_SELECTED[self.category]:
            return False
        if self.auto or self.category in ("person", "organization", "domain"):
            return True
        # Text-only project guesses are often public products (Slack, Zoom): you tick the internal ones.
        return False

    @property
    def replacement(self) -> str:
        return REPLACEMENT[self.category]

    def describe(self) -> str:
        bits = []
        if self.count:
            bits.append(f"{self.count}×")
        bits.append(", ".join(sorted(self.evidence)))
        if self.auto:
            bits.append("detected automatically")
        if self.cased:
            bits.append("only when capitalized")
        return " · ".join(b for b in bits if b)


@dataclass
class Findings:
    candidates: dict[str, list[Candidate]]  # category -> ranked candidates
    sources: dict[str, int]
    errors: list[str]
    # Auto-scrubbed names that never appear in exportable text, by category; listed only as a count.
    unmentioned_auto: dict[str, int] = field(default_factory=dict)


class _Bag:
    def __init__(self) -> None:
        self.by_key: dict[tuple[str, str], Candidate] = {}

    def add(self, term: str, category: str, evidence: str, count: int = 0, auto: bool = False,
            cased: bool = False) -> None:
        term = term.strip().strip(".,;:()[]\"'")
        if len(term) < 2:
            return
        key = (category, term.lower())
        c = self.by_key.get(key)
        if c is None:
            c = self.by_key[key] = Candidate(term=term, category=category)
        c.count += count
        c.evidence.add(evidence)
        c.auto = c.auto or auto
        c.cased = c.cased or cased


def looks_like_a_word(term: str) -> bool:
    """A one-word scrub term that's more likely an ordinary or technical word than a name
    ("Metadata", "Org", "LinkedIn"): used to untick stale People entries carried over from a config."""
    if " " in term.strip():
        return False
    t = term.strip().lstrip("@")
    low = t.lower()
    words = dictionary_words()
    ordinary = (low in words or (low.endswith("s") and low[:-1] in words)
                or any(low.endswith(suf) and low[: -len(suf)] in words for suf in ("es", "ed", "ing", "ly")))
    return (ordinary and low not in first_names()) or low in NOT_SURNAME or low in TECH or low in UPPER_WORDS \
        or _camel(t)


def _is_bot(name: str, email: str) -> bool:
    n = name.lower()
    return ("[bot]" in n or "bot@" in email or n.endswith("bot") or n in ("github", "github actions", "claude")
            or "noreply@anthropic" in email or "dependabot" in n or "renovate" in n)


SYSTEM_ACCOUNTS = {
    "root", "admin", "administrator", "ubuntu", "debian", "centos", "ec2-user", "vagrant", "jenkins", "deploy",
    "deployer", "git", "build", "builder", "ci", "automation", "operations", "ops", "devops", "system", "service",
    "server", "user", "test", "tester", "developer", "dev", "www-data", "nobody", "unknown", "localhost", "pi",
}


def _not_a_person(name: str, words: set[str]) -> bool:
    """System accounts and one-word ordinary names ("root", "Operations", "Ubuntu") aren't people."""
    tokens = name.split()
    low = name.lower().strip()
    if low in SYSTEM_ACCOUNTS or low in TECH or "@" in name or "localhost" in low:
        return True
    return len(tokens) == 1 and (low in words or low.rstrip("s") in words)


def _registrable(domain: str) -> str:
    labels = domain.lower().strip(".").split(".")
    if len(labels) >= 3 and len(labels[-2]) <= 3 and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def discover(cfg: Config, plan: Plan) -> Findings:
    got = collect(cfg, plan)
    bag = _Bag()
    words = dictionary_words()
    given = first_names()
    user_terms = {k.lower() for k, v in cfg.redact_terms.items() if v == "the user"}
    if plan.repos:
        for repo in instructions.repos_to_read(cfg.repo_roots, cfg.only_my_repos):
            user_terms |= {e.split("@")[0] for e in instructions.repo_emails(repo)}
    allow = [d.lower() for d in cfg.allow_domains]

    user_terms |= {k.lower() for k, v in config_mod.auto_redact_terms().items() if v == "the user"}

    former = cfg.former

    def is_user(term: str) -> bool:
        # A former name is you: never listed (so it isn't shown on screen) and scrubbed as "the user".
        if former and (former.matches(term) or former.matches(term.lstrip("@"))
                       or (len(term.split()) == 1 and any(former.matches(p) for p in re.split(r"[._@+\-]", term) if p))):
            return True
        t = term.lower().lstrip("@")
        return t in user_terms or all(p in user_terms for p in t.split())

    author_names: set[str] = set()

    # --- structured sources ----------------------------------------------------------------
    for name in got.people:
        # Skip templates and index notes ("person", "README", "_index").
        if name[:1].isupper() and name.lower() not in words and not name.startswith("_"):
            bag.add(name, "person", "People folder", auto=True)
    if plan.claude:
        for name in claude_memory.project_names(cfg.claude_dir):
            if name.lower() not in GENERIC_NAMES:
                bag.add(name, "project", "Claude project", auto=True)
    if plan.repos:
        for repo in instructions.repos_to_read(cfg.repo_roots, cfg.only_my_repos):
            if (repo.name.lower() not in GENERIC_NAMES and (len(repo.name) >= 5 or "-" in repo.name)
                    and not config_mod.common_word(repo.name)):
                bag.add(repo.name, "project", "repo name", auto=True)
            owners, domains = instructions.repo_identity(repo)
            for o in owners:
                bag.add(o, "organization", "git remote owner", auto=True)
            for d in domains:
                org = org_from_domain(d)
                if org:
                    bag.add(org, "organization", "repo git email", auto=True)
            for name, email in instructions.git_authors(repo):
                if not name or _is_bot(name, email) or _not_a_person(name, words):
                    continue
                bag.add(name, "person", "git author")
                author_names.add(name)
                dom = email.split("@")[-1]
                if "noreply" not in dom:
                    org = org_from_domain(dom)
                    if org:
                        bag.add(org, "organization", "git author email")

    # --- the text itself -------------------------------------------------------------------
    corpus = "\n\n".join(f"{i.title}\n{i.summary}\n{i.body}" for i in got.items)
    prose = CODE.sub(" ", corpus)
    lowercase_seen = {w.lower() for w in re.findall(r"\b[a-z][a-z]{3,}\b", prose)}

    def ordinary(word: str) -> bool:
        w = word.lower()
        return (w in words or any(w.endswith(suf) and w[: -len(suf)] in words for suf in ("s", "es", "ed", "d", "ing", "ly"))
                or (w.endswith("ied") and w[:-3] + "y" in words)
                or (w.endswith(("ing", "ed")) and w[: -3 if w.endswith("ing") else -2] + "e" in words)
                or w.rstrip("s") in CALENDAR)

    # --- people: learn which words are parts of names ------------------------------------------
    # The system's given-name list is small and mostly Anglo, so it can't be the only evidence.
    # A word is a name part if it's in a name from your People folder or a git author's name, or
    # it sits next to one in "First Last". Those words, used alone in the text ("ask Quinn"), are
    # people, never projects.
    name_parts: dict[str, str] = {}

    def strip_poss(w: str) -> str:
        return re.sub(r"['’]s?$", "", w)

    cased_parts: set[str] = set()

    def learn(full: str, evidence: str, allow_words: bool = True) -> None:
        """Record the words of a known name. A word that's also ordinary English ("Smith",
        "Young", "Rose") is kept, but marked to be scrubbed only when capitalized."""
        for tok in full.split():
            tok = strip_poss(tok.strip(".,()[]\"“”"))
            low = tok.lower()
            if not (len(tok) >= 3 and tok[:1].isupper() and low not in TECH and low not in CALENDAR
                    and low not in NOT_SURNAME and low not in UPPER_WORDS and not _camel(tok)):
                continue
            if ordinary(tok):
                if not allow_words:
                    continue
                cased_parts.add(low)
            name_parts.setdefault(low, evidence)

    for full in got.people:
        learn(full, "part of a name in your People folder")
    for full in author_names:
        learn(full, "part of a git author's name")
    pairs = Counter((a, strip_poss(b)) for a, b in NAME_PAIR.findall(prose))
    for (a, b), n in pairs.items():
        if b.lower() in NOT_SURNAME or b.lower() in TECH or a.lower() in TECH or a.lower() in CALENDAR:
            continue
        known = a.lower() in name_parts or b.lower() in name_parts
        # A given name that's also a word (Grace, Frank, Will) counts when the surname isn't one.
        if known or (a.lower() in given and (not ordinary(a) or not ordinary(b))):
            bag.add(f"{a} {b}", "person", "named in text", n)
            learn(f"{a} {b}", "next to a known name in the text", allow_words=False)
    for first, last, dom in FIRST_LAST_EMAIL.findall(corpus):
        if first.lower() in given or first.lower() in name_parts:
            bag.add(f"{first.capitalize()} {last.capitalize()}", "person", "email address")
    capitalized: Counter = Counter()
    spelled: dict[str, Counter] = {}
    for w in re.findall(r"\b[A-Z][\w'’\-]+", prose):
        w = strip_poss(w)
        capitalized[w.lower()] += 1
        spelled.setdefault(w.lower(), Counter())[w] += 1

    def as_written(low: str) -> str:
        forms = spelled.get(low)
        return forms.most_common(1)[0][0] if forms else (low.capitalize() if low.islower() else low)
    for low, evidence in name_parts.items():
        n = capitalized.get(low, 0)
        if n:
            bag.add(as_written(low), "person", evidence, n, cased=low in cased_parts)
    used_like_a_name = Counter(strip_poss(m).lower() for pat in (PERSON_BEFORE, PERSON_AFTER)
                               for m in pat.findall(prose)
                               if not CONTRACTION.match(m) and not m.isupper() and not _camel(strip_poss(m)))
    for low, hits in used_like_a_name.items():
        if (low not in name_parts and len(low) >= 3 and not ordinary(low) and low not in TECH
                and low not in CALENDAR and low not in NOT_SURNAME and "'" not in low and "’" not in low):
            name_parts[low] = "used like a name in the text"
            bag.add(as_written(low), "person", "used like a name in the text", capitalized.get(low, hits))
    for low, n in capitalized.items():
        if (n >= 1 and len(low) >= 3 and low in given and low not in name_parts and not ordinary(low)
                and low not in TECH and low not in CALENDAR):
            bag.add(low.capitalize(), "person", "a common given name", n)

    for handle in Counter(h for h in HANDLE.findall(prose)):
        low = handle.lower().rstrip(".")
        if "." in low and low.rsplit(".", 1)[-1] in COMMON_TLDS:
            continue  # "@acme.edu" is a domain written without a name in front: see Domains
        if low in TECH or low in NOT_HANDLES or _is_bot(low, ""):
            continue
        bag.add("@" + handle.rstrip("."), "person", "@handle", prose.count("@" + handle))

    for dom in EMAIL.findall(corpus):
        org = org_from_domain(dom.lower())
        if org:
            bag.add(org, "organization", "email domain", 1)
    for m in ORG_SUFFIX.finditer(prose):
        phrase = re.sub(r"^(?:The|A|An|At|In|Of|For|With|From|And)\s+", "", m.group(0).rstrip("."))
        if len(phrase.split()) >= 2:
            bag.add(phrase, "organization", "named in text", 1)
    for m in UNIVERSITY_OF.finditer(prose):
        bag.add(m.group(1), "organization", "named in text", 1)

    for term, n in Counter(CAMEL.findall(prose)).items():
        if (n >= 2 and term.lower() not in TECH and not term.lower().startswith(("get", "set", "is", "has", "to"))
                and not CODE_SUFFIX.search(term) and term.lower() not in name_parts):
            bag.add(term, "project", "CamelCase name", n)
    cap_counts = Counter(CAPWORD.findall(prose))
    for term, n in cap_counts.items():
        low = term.lower()
        if (n >= 3 and low not in lowercase_seen and not ordinary(term) and low not in TECH and low not in given
                and low not in name_parts and low not in CALENDAR):
            bag.add(term, "other", "capitalized, never lowercase", n)

    for term, n in Counter(ACRONYM.findall(prose)).items():
        low = term.lower()
        letters = sum(ch.isalpha() for ch in term)
        plural_word = low.endswith("s") and low[:-1] in words
        if (n >= 3 and letters >= 3 and sum(ch.isdigit() for ch in term) <= 1 and low not in TECH
                and low not in UPPER_WORDS and low not in words and not plural_word):
            bag.add(term, "acronym", "acronym", n)

    hosts = ([m.group(1) for m in URL_HOST.finditer(corpus)] + EMAIL.findall(corpus)
             + [h for h in HANDLE.findall(prose) if "." in h and h.rstrip(".").rsplit(".", 1)[-1].lower() in COMMON_TLDS])
    for full in hosts:
        if full.rsplit(".", 1)[-1].lower() not in COMMON_TLDS:
            continue
        reg = _registrable(full)
        if reg in PUBLIC_DOMAINS or any(reg == d or reg.endswith("." + d) or d.endswith("." + reg) for d in allow):
            continue
        if reg.split(".")[0] in PUBLIC_MAIL:
            continue
        bag.add(reg, "domain", "URLs and emails", 1)

    # --- rank and tidy -----------------------------------------------------------------------
    # Count text mentions for structured finds too, so the list shows how much each one matters.
    low_corpus = corpus.lower()
    for c in bag.by_key.values():
        if c.count == 0:
            c.count = len(re.findall(r"(?<![a-z0-9])" + re.escape(c.term.lower()) + r"(?![a-z0-9])", low_corpus))

    out: dict[str, list[Candidate]] = defaultdict(list)
    unmentioned: Counter = Counter()
    taken: set[str] = set()
    priority = ["person", "organization", "project", "domain", "acronym", "other"]
    for cat in priority:
        cands = [c for (k, _), c in bag.by_key.items() if k == cat]
        cands = [c for c in cands if not is_user(c.term) and c.term.lower() not in taken]
        # Only what appears in the text an export draws on can leak. Auto-scrubbed names that
        # don't appear are counted, not listed, to keep the list short.
        unmentioned[cat] += sum(1 for c in cands if c.count == 0 and c.auto)
        cands = [c for c in cands if c.count > 0]
        cands.sort(key=lambda c: (-c.count, not c.auto, c.term.lower()))
        # Claim every term in this category, listed or not, so it can't resurface in a later one.
        taken |= {c.term.lower() for c in cands}
        out[cat].extend(cands[: LIMITS[cat]])
    return Findings(candidates=dict(out), sources=got.sources, errors=got.errors,
                    unmentioned_auto={k: v for k, v in unmentioned.items() if v})
