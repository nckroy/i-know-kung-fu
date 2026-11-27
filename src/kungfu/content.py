# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Content that shouldn't leave your machine even after names and numbers are scrubbed.

None of this is rewritten. Rewriting a rant, a quote or a security finding leaves something
misleading behind, and only you can judge whether a remark about a vendor is fair. So each match
holds the learning back, with a reason you can read in review, and raises its sensitivity score.

Categories:
  reputation    profanity, slurs, negative judgments about people or organizations, quoted
                private messages, venting or emotional state, gossip
  confidential  handling markings (TLP:RED/AMBER, CUI, privileged…), security posture (unpatched
                or exploitable systems, pen-test and audit findings, incidents), business matters
                (pricing, contracts, budgets, negotiations, unreleased plans)
  personal      answers to security questions, whereabouts and schedules, family members,
                other people's personal details
  copied        copyright notices, long quoted passages
  language      text that is mostly not English (these checks only understand English)

Checks run on scrubbed text, so placeholders such as [person] and "the organization" help: a
negative judgment counts when it's about a person or an organization, not about code.
"""

from __future__ import annotations

import re

from kungfu import wordlist


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


# --- reputation ----------------------------------------------------------------------------

# Profanity and slurs, including censored spellings, are matched by hash in kungfu.wordlist, so the
# words aren't written here.


# Words that judge a person ("clueless") count after a pronoun too; general complaints ("useless",
# "a mess") only after an explicit person or organization, so "it's useless without auth" is fine.
PERSONAL_NEG = (r"(?:incompetent|clueless|idiots?|idiotic|morons?|moronic|stupid|dumb|lazy|worthless|pathetic|"
                r"inept|a joke|a clown|clowns|toxic|shady|sketchy|untrustworthy|liars?|dishonest|arrogant|"
                r"condescending|insufferable|dead ?weight|unprofessional|difficult to work with|a pain to work with)")
GENERAL_NEG = (r"(?:useless|hopeless|garbage|trash|a nightmare|a disaster|a mess|so bad|the worst|awful|terrible|"
               r"crazy|nuts|annoying|a pain)")
NAMED = (r"(?:\[person\]|\[name\]|the organization|the vendor|vendors|the customer|customers|the client|clients|"
         r"my boss|the boss|the manager|my manager|management|leadership|that team|their support|the contractor|"
         r"contractors|the consultants?)")
PRONOUN = r"(?:he|she|they|his team|her team|their team)"
_IS = (r"\s+(?:is|are|was|were|seems?|seemed|'s|’s|'re|’re|has been|have been|remains?|looks?|sounds?)\s+"
       r"(?:so |really |totally |completely |utterly |just |honestly |pretty )?")
JUDGMENT = [
    re.compile(r"(?<!\w)" + NAMED + _IS + r"(?:" + PERSONAL_NEG + r"|" + GENERAL_NEG + r")\b", re.I),
    re.compile(r"(?<!\w)" + PRONOUN + _IS + PERSONAL_NEG + r"\b", re.I),
    re.compile(r"\b(?:that|this|what an?|such an?|those|these)\s+(?:" + PERSONAL_NEG + r"|" + GENERAL_NEG + r")\s+"
               r"(?:person|people|guy|guys|woman|man|vendor|team|manager|boss|colleague|client|customer|contractor|"
               r"consultant|engineer)s?\b", re.I),
    re.compile(r"\b(?:incompetent|clueless|lazy|idiotic|moronic|inept|toxic|dishonest|arrogant)\s+"
               r"(?:person|people|guy|guys|woman|man|vendor|team|manager|boss|colleague|client|customer|"
               r"contractor|consultant|engineer|admin|staff)s?\b", re.I),
]
QUOTED = [
    # A message attributed to someone: "Nicole, 2026-11-19: *"…"*", "X said: "…"", "(DM 14:14)"
    re.compile(r"(?:\[person\]|the user|[A-Z][a-z]+),?\s+\d{4}-\d{2}-\d{2}\s*:\s*\*?[\"“]"),
    re.compile(r"\b(?:said|wrote|replied|messaged|emailed|posted|texted|DM'?d|asked|told (?:me|us|the user))\s*"
               r"(?:in Slack|on Slack|by email|in (?:a |the )?DM)?\s*[:,]?\s*[*_]?[\"“][^\"”\n]{12,}", re.I),
    re.compile(r"[*_][\"“][^\"”\n]{20,}[\"”][*_]"),  # *"quoted message"*, the vault's quoting style
    re.compile(r"\b(?:Slack|DM|email|Teams|text) (?:from|to) (?:\[person\]|[A-Z][a-z]+)[^\n]{0,40}[\"“]", re.I),
]
VENTING = [
    re.compile(r"\b(?:the user|I|she|he|they|\[person\]|we)\s+(?:was|is|were|are|got|gets|get|felt|feels?|seemed|"
               r"seems|'m|am|'re|’m|’re)\s+(?:very |really |so |extremely |getting |increasingly |clearly )?"
               r"(?:frustrated|annoyed|angry|upset|furious|irritated|exasperated|fed up|livid|"
               r"burn(?:ed|t) out|overwhelmed|stressed(?: out)?|exhausted|miserable|in tears|crying|losing (?:my|her|"
               r"his|their) mind)\b", re.I),
    re.compile(r"\b(?:rant|venting|vent)\s*[:—-]", re.I),
    re.compile(r"\b(?:sick (?:and tired )?of|hate (?:working|this job|my job|them|him|her)|can'?t stand)\b", re.I),
]
GOSSIP = _rx(
    r"(?:\b(?:rumou?rs?|rumou?red|gossip|between (?:you and me|us)|off the record|don'?t tell (?:anyone|them)|"
    r"word is that|heard through the grapevine|about to (?:quit|be fired))\b|"
    r"(?:\bhe|\bshe|\[person\]|\[name\])\s+(?:is|was)\s+(?:leaving|quitting|being let go|getting fired|on a PIP|"
    r"on a performance plan|interviewing elsewhere|job[- ]hunting)\b)")

# --- confidential --------------------------------------------------------------------------

MARKINGS = _rx(
    r"(?:\bTLP\s*:\s*(?:RED|AMBER(?:\+STRICT)?)\b|\bCUI\b|\bFOUO\b|for official use only|privileged (?:and|&) "
    r"confidential|attorney[- ]client|legal(?:ly)? privilege[d]?|\bembargo(?:ed)?\b|under embargo|do not distribute|"
    r"not for (?:distribution|circulation)|proprietary (?:and|&) confidential|trade secret|company confidential|"
    r"sensitive but unclassified|need[- ]to[- ]know basis)")
SECURITY_TERMS = _rx(
    r"\b(?:unpatched|not (?:yet )?patched|vulnerable to|exploitable|zero[- ]day|0[- ]?day|pen(?:etration)?[- ]?test "
    r"(?:finding|report|result)s?|audit (?:finding|exception)s?|security finding|finding [A-Z]?\d+\b|"
    r"incident (?:report|response|ticket|timeline)|breach(?:ed)?|compromised (?:account|host|server|credentials?|"
    r"key)s?|exposed (?:to the internet|publicly)|no MFA|without MFA|backdoor|bypass(?:es|ed)? auth\w*|"
    r"privilege escalation|CVE-\d{4}-\d{4,})")
OURS = _rx(r"(?:\b(?:our|we|we're|we are|us|ours|prod|production|staging|internal(?:ly)?|the organization)\b|"
           r"\[project\]|\[internal-host\]|\[internal-url\]|\[ip\])")
# Money talk needs an amount in the same sentence; deal talk needs it to be ours.
MONEY_TERMS = _rx(r"\b(?:pricing|price[ds]?|quoted? us|discount(?:ed|s)?|renewal|budget(?:ed|s)?|revenue|invoice[ds]?|"
                  r"purchase order|we pay|cost(?:s)? us|annual fee|license fee|per[- ]seat|salary|rate card)\b")
DEAL_TERMS = _rx(r"\b(?:negotiat\w+|term sheet|merger|M&A|headcount|contract (?:value|terms|renewal)|our contract|"
                 r"the contract with|go[- ]live date|launch date)\b")
MONEY = re.compile(r"(?:[$€£¥]\s?\d[\d,.]*\s?(?:k|m|mm|million|billion)?\b|\b\d[\d,.]*\s?(?:k|m)?\s?(?:USD|EUR|GBP|CAD|"
                   r"dollars|euros|pounds)\b)", re.I)
UNRELEASED = _rx(r"\b(?:unannounced|not yet announced|stealth (?:mode|project)|internal roadmap|confidential roadmap|"
                 r"under NDA)\b")

# --- personal life and safety ------------------------------------------------------------------

SECURITY_QUESTIONS = _rx(
    r"\b(?:mother'?s maiden name|maiden name|first pet|pet'?s name|first car|(?:my|her|his) high school|elementary "
    r"school|childhood (?:best )?friend|street (?:I|you|she|he) grew up on|city (?:I|you|she|he) (?:was|were) born|"
    r"favou?rite teacher|(?:my|her|his) security answers?)\b")
WHEREABOUTS = _rx(
    r"\b(?:out of (?:the )?office (?:until|from|through|till)|OOO (?:until|from|through|till|on)|on (?:vacation|"
    r"holiday|leave|PTO|medical leave|parental leave)|(?:I'm|I am|she's|she is|he's|he is|the user is|\[person\] is) "
    r"(?:out of town|travel(?:l)?ing|flying|on the road)|away until|back (?:on|in the office) "
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|\d)|home alone|house[- ]?sitting|while (?:I'm|I am|she's|he's) "
    r"away)\b")
FAMILY = _rx(
    r"\b(?:my|her|his|the user'?s|\[person\]'?s)\s+(?:wife|husband|partner|spouse|fiancée?|girlfriend|"
    r"boyfriend|son|daughter|kids?|child(?:ren)?|mother|mom|mum|father|dad|sister|brother|parents|grandmother|"
    r"grandfather|ex|in-laws?|family)\b")
OTHERS_DETAILS = _rx(
    r"\[person\]'?s\s+(?:health|illness|diagnosis|pregnancy|divorce|wife|husband|partner|kids?|children|salary|"
    r"pay|debt|finances|religion|sexuality|address|home|phone|medical|surgery|therapy|arrest|lawsuit)\b")

# --- copied material and language ----------------------------------------------------------------

COPYRIGHT = _rx(r"(?:©\s*\d{4}|\bcopyright\s+(?:©\s*)?\d{4}|all rights reserved|reproduced (?:with|by) permission|"
                r"reprinted with permission)")
BLOCKQUOTE = re.compile(r"(?:^>[^\n]*\n?)+", re.M)
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def _english_share(text: str) -> tuple[int, float]:
    from kungfu.config import dictionary_words

    words = dictionary_words()
    prose = CODE.sub(" ", text)
    tokens = [w for w in re.findall(r"\b[^\W\d_]{3,}\b", prose) if w.islower() or w.istitle()]
    if not tokens:
        return 0, 1.0
    known = sum(1 for w in tokens if w.lower() in words or w.lower().rstrip("s") in words
                or w.lower().endswith(("ed", "ing", "ly", "es")) and w.lower()[:-2] in words)
    return len(tokens), known / len(tokens)


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+|\n+", text)


OWN_QUOTE = re.compile(r"(?:the user|she|I)\b[^\n\"“]{0,40}$", re.I)


def _others_quoted(prose: str, ai_notes: bool) -> bool:
    """Quotes of other people's words. In Claude's memories and instruction files, quotes are usually the
    exporter's own instructions to Claude; those don't count unless attributed to someone else."""
    for pat in QUOTED:
        for m in pat.finditer(prose):
            before = prose[max(0, m.start() - 60):m.start()] + m.group(0)[:20]
            if ai_notes and (OWN_QUOTE.search(prose[max(0, m.start() - 60):m.start() + 1])
                             or not re.search(r"\[person\]|\[name\]|[A-Z][a-z]+ (?:said|wrote|replied|asked)", before)):
                continue
            if m.group(0).lower().startswith("the user") or re.search(r"\bthe user,?\s+\d{4}-\d{2}-\d{2}", before):
                if ai_notes:
                    continue
            return True
    return False


def review(text: str, ai_notes: bool = False) -> list[tuple[str, str]]:
    """(category, detail) for everything in the text that should keep a learning on this machine.

    ``ai_notes``: the text is a Claude memory or an agent instruction file, where quotes are mostly the
    exporter's own instructions and blockquotes are status notes rather than copied passages."""
    out: list[tuple[str, str]] = []

    def add(cat: str, detail: str) -> None:
        if (cat, detail) not in out:
            out.append((cat, detail))

    prose = CODE.sub(" ", text)
    # reputation
    blocked = wordlist.categories(prose)
    if "profanity" in blocked:
        add("reputation", "profanity")
    if "slur" in blocked:
        add("reputation", "a slur")
    if any(p.search(prose) for p in JUDGMENT):
        add("reputation", "a negative judgment about a person or organization")
    if _others_quoted(prose, ai_notes):
        add("reputation", "quotes someone's private words")
    if any(p.search(prose) for p in VENTING):
        add("reputation", "venting or someone's emotional state")
    if GOSSIP.search(prose):
        add("reputation", "gossip or speculation about people")
    # confidential
    if MARKINGS.search(prose):
        add("confidential", "a confidentiality or handling marking")
    for sentence in _sentences(prose):
        if SECURITY_TERMS.search(sentence) and OURS.search(sentence):
            add("confidential", "details of a security weakness or incident")
        if (MONEY_TERMS.search(sentence) and MONEY.search(sentence)) or (DEAL_TERMS.search(sentence) and OURS.search(sentence)):
            add("confidential", "business details (pricing, contracts, budgets, plans)")
    if UNRELEASED.search(prose):
        add("confidential", "unreleased plans")
    # personal
    if SECURITY_QUESTIONS.search(prose):
        add("personal", "an answer to a security question")
    if WHEREABOUTS.search(prose):
        add("personal", "someone's whereabouts or schedule")
    if FAMILY.search(prose):
        add("personal", "family members")
    if OTHERS_DETAILS.search(prose):
        add("personal", "another person's personal details")
    # copied material
    if COPYRIGHT.search(text):
        add("copied", "a copyright notice")
    if not ai_notes and any(len(m.group(0)) >= 500 and not m.group(0).startswith("> [!")
                            for m in BLOCKQUOTE.finditer(text)):
        add("copied", "a long quoted passage")
    # language
    n, share = _english_share(text)
    if n >= 20 and share < 0.45:
        add("language", "mostly not English, which these checks can't read")
    return out


WEIGHTS = {"reputation": 3.0, "confidential": 3.0, "personal": 3.0, "copied": 2.0, "language": 2.0}
