# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Scrub learnings before they leave the machine.

Three layers, each recorded as a flag the reviewer sees:

1. Secrets (API keys, tokens, private keys, password assignments, credentials in URLs, high-entropy
   strings) are replaced with ``[REDACTED:...]`` and the learning is held back by default.
2. Personal and internal details (email addresses, phone numbers, IP addresses, home directories,
   internal hostnames, configured names and people's names from the vault) are replaced.
3. Sensitive subjects (HR, medical, hiring, confidential...) hold the learning back by default.

The same scan runs again on the final text at pack time, and on import as a warning.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from kungfu import content, identifiers
from kungfu.model import Learning

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
                               r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)")),
    ("private-key", re.compile(r"PuTTY-User-Key-File-\d+:[\s\S]*?(?:Private-MAC:\s*[0-9a-fA-F]+|\Z)")),
    ("pgp-message", re.compile(r"-----BEGIN PGP MESSAGE-----[\s\S]*?(?:-----END PGP MESSAGE-----|\Z)")),
    ("password-hash", re.compile(r"(?<![\w$])\$(?:1|2[abxy]?|5|6|7|9|y|gy|argon2(?:id|i|d)?|scrypt|pbkdf2[\w-]*)\$"
                                 r"[./A-Za-z0-9$=,+\-]{8,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe-key", re.compile(r"\b[sr]k_(?:live|test)_[0-9a-zA-Z]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("gitlab-token", re.compile(r"\bgl(?:pat|dt|rt|ptt|ft|cbt|imt|oas|soat)-[A-Za-z0-9_\-]{20,}")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")),
    ("vault-token", re.compile(r"\bhv[sbr]\.[A-Za-z0-9_\-]{20,}")),
    ("digitalocean-token", re.compile(r"\bdo[por]_v1_[a-f0-9]{64}\b")),
    ("twilio-key", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("mailgun-key", re.compile(r"\bkey-[0-9a-zA-Z]{32}\b")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("databricks-token", re.compile(r"\bdapi[0-9a-f]{32}(?:-\d)?\b")),
    ("shopify-token", re.compile(r"\bshp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}\b")),
    ("atlassian-token", re.compile(r"\bATATT3[A-Za-z0-9_\-=]{50,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi-token", re.compile(r"\bpypi-AgE[A-Za-z0-9_\-]{16,}")),
    ("ansible-vault", re.compile(r"\$ANSIBLE_VAULT;\d\.\d;AES256(?:;[\w-]+)?[ \t]*\n(?:[ \t]*[0-9a-f]+[ \t]*(?:\n|\Z))+")),
    ("bitlocker-recovery-key", re.compile(r"\b\d{6}(?:-\d{6}){7}\b")),
    ("filevault-recovery-key", re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]{4}(?:-[A-Z0-9]{4}){5}\b")),
    ("license-key", re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}\b")),
    ("crypto-private-key", re.compile(r"\b(?:[5KL][1-9A-HJ-NP-Za-km-z]{50,51}|xprv[1-9A-HJ-NP-Za-km-z]{100,112})\b")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_\-]+")),
    ("discord-webhook", re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+")),
]

DOCKER_AUTH = re.compile(r'("(?:auth|identitytoken|registrytoken)"\s*:\s*")([A-Za-z0-9+/=._-]{12,})(")')
LABELLED_LICENSE = re.compile(r"(?i)\b((?:license|licence|product|serial|activation|registration|recovery)[ \t]+key"
                              r"[ \t]*[:=#]?[ \t]*)([A-Z0-9]{4,6}(?:-[A-Z0-9]{4,6}){2,7})\b")
# Labels that name a secret. They're found on their own ("password: …") and inside identifiers
# ("DB_PASSWORD=…", "apiToken: …", "X-Auth-Token: …", "aws_secret_access_key = …").
STRONG_LABELS = (r"password|passwd|passcode|passphrase|pwd|pw|pass|secret|shared[_\- ]?secret|psk|"
                 r"pre[_\- ]?shared[_\- ]?key|wep[_\-]?key|sae[_\-]?password")
TOKEN_LABELS = (r"api[_\-]?key|apikey|access[_\-]?key|secret[_\-]?key|private[_\-]?key|client[_\-]?secret|"
                r"auth[_\-]?token|access[_\-]?token|refresh[_\-]?token|id[_\-]?token|session[_\-]?(?:id|token|key)|"
                r"token|bearer|cookie|credentials?|account[_\-]?key|shared[_\-]?access[_\-]?key|sas[_\-]?token|"
                r"signing[_\-]?key|encryption[_\-]?key|master[_\-]?key|webhook[_\-]?secret|otp|totp[_\-]?secret|"
                r"client[_\-]?key[_\-]?data|storepass|keypass|keystore[_\-]?pass(?:word)?")
LABELLED_SECRET = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_.\-]|(?-i:[a-z][a-zA-Z0-9]*?(?=[A-Z][a-z]))|(?-i:[a-z][a-z0-9]*(?=[A-Z]))|"
    r"(?-i:[A-Z][A-Z0-9]*(?=(?:PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|APIKEY|PSK)\b)))*(" + STRONG_LABELS + r"|" + TOKEN_LABELS + r")"
    r"((?:[_.\-][A-Za-z0-9]+|(?-i:[A-Z][a-z0-9]+))*))"
    r"(?![A-Za-z0-9])"
    r"(\s*(?:=>|:=|[:=])\s*|\s+(?:is|was|=)\s+|[ \t]+)"
    r"(?:(\"[^\"\n]{3,200}\"|'[^'\n]{3,200}')|([^\s\"'`<>,;)\]]{3,200}))"
)
# A label that ends in one of these names the secret or describes it; its value isn't the secret.
DESCRIPTORS = {
    "name", "names", "id", "ids", "arn", "path", "file", "files", "ref", "refs", "version", "type", "length", "len",
    "ttl", "count", "policy", "rotation", "manager", "store", "engine", "backend", "provider", "env", "var", "header",
    "field", "label", "prefix", "suffix", "url", "uri", "endpoint", "location", "dir", "format", "hint", "expiry",
    "expires", "expiration", "timeout", "lifetime", "limit", "min", "max", "size", "algorithm", "alg", "hash",
    "scope", "scopes", "mount", "source", "kind", "mode", "enabled", "required", "reset", "changed", "updated",
}
PLACEHOLDER = re.compile(r"^(?:\$|\{\{|<|%|\[REDACTED|!|\*{3}|x{3,}$|\.\.\.|…)", re.I)
NOT_A_VALUE = {
    "required", "optional", "none", "null", "nil", "true", "false", "empty", "redacted", "hidden", "set", "unset",
    "yes", "no", "on", "off", "default", "string", "str", "bytes", "text", "secret", "password", "token", "value",
    "env", "environment", "variable", "example", "changeit",
}
Q = r"""('[^'\n]*'|"[^"\n]*"|[^\s'"]+)"""
CLI_SECRETS = [
    re.compile(r"(\bssh-keygen\b[^\n]*?\s-[NP]\s*)" + Q),
    re.compile(r"(\bsshpass\s+-p\s*)" + Q),
    re.compile(r"(?i)(\B--(?:passphrase|password|passwd|pass|secret|token|api-key|apikey|client-secret|psk)(?:=|[ \t]+))" + Q),
    re.compile(r"(\b(?:mysql|mysqldump|mysqladmin|mariadb|mariadb-dump)\b[^\n]*?\s-p)([^\s'\"]+)"),
    re.compile(r"(-pass(?:in|out)?\s+pass:)(\S+)"),
    re.compile(r"(\s-(?:storepass|keypass|srcstorepass|deststorepass|srckeypass|destkeypass)\s+)" + Q),
    re.compile(r"(\bcurl\b[^\n]*?\s(?:-u|--user)\s+'?[^\s:']+:)([^\s']+)"),
]
# Network devices: Cisco "radius-server key 7 …", "radius server X" blocks with " key …",
# "tacacs-server key …", "key-string 7 …", "pre-shared-key …", "password 7 <type-7 hex>".
DEVICE_KEYS = [
    re.compile(r"(?i)(\b(?:radius|tacacs\+?)[- ](?:server|host)\b[^\n]*?\s(?:key|secret)\s+(?:[0-9]\s+)?)(\S{4,})"),
    re.compile(r"(?i)(\b(?:radius|tacacs\+?)\s+server\s+\S+[^\n]*\n(?:[ \t]+[^\n]*\n){0,8}?[ \t]+(?:key|secret)\s+(?:[0-9]\s+)?)(\S{4,})"),
    re.compile(r"(?i)(\b(?:key-string|pre-shared-key|server-private\s+\S+\s+key)\s+(?:[0-9]\s+)?)(\S{4,})"),
    re.compile(r"(?i)(\b(?:password|secret|key)\s+[57]\s+)([0-9A-Fa-f$./]{8,})"),
]
# Config directives with no "=": radsecproxy/FreeRADIUS "secret mysharedsecret", "psk …" on their
# own line, or inside braces before ";" or "}".
CONFIG_DIRECTIVE = re.compile(
    r"(?im)(^[ \t]*(?:secret|sharedsecret|shared_secret|password|passwd|psk|passphrase)[ \t]+)"
    r"(\"[^\"\n]+\"|'[^'\n]+'|[^\s;{}#]+)(?=[ \t]*;?[ \t]*(?:#.*)?$)")
BRACE_DIRECTIVE = re.compile(
    r"(?i)([{;][ \t]*(?:secret|sharedsecret|shared_secret|password|passwd|psk)[ \t]+)(\"[^\"\n]+\"|[^\s;{}]+)(?=[ \t]*[;}])")
# Identifiers of keys. Not secret, but they tie text to a person's key; replaced, not held back.
KEY_FINGERPRINT = [
    re.compile(r"\bSHA256:[A-Za-z0-9+/]{43}=?"),
    re.compile(r"(?<![:\w])(?:MD5:)?(?:[0-9a-f]{2}:){15}[0-9a-f]{2}(?![:\w])"),
    re.compile(r"(?i)(\b(?:signing\s*key|signingkey|key\s*id|keyid|fingerprint|gpg\s+key|pgp\s+key|"
               r"default-key|local-user|-u)\b[ \t]*[:=]?[ \t]*)((?:0x)?[0-9A-F]{4}(?:[ ]{0,2}[0-9A-F]{4}){1,9})\b"),
]
PUBLIC_KEY_BLOCK = re.compile(r"-----BEGIN PGP PUBLIC KEY BLOCK-----[\s\S]*?(?:-----END PGP PUBLIC KEY BLOCK-----|\Z)")
SSH_PUBLIC_KEY = re.compile(r"\b((?:ssh-(?:rsa|dss|ed25519)|ecdsa-sha2-nistp\d+|sk-ssh-ed25519@openssh\.com|"
                            r"sk-ecdsa-sha2-nistp256@openssh\.com)\s+)AAAA[0-9A-Za-z+/]{20,}={0,3}")

PIN = re.compile(r"(?i)\b(pin(?:[ \t]*(?:code|number|#))?[ \t]*[:=#]?[ \t]*)(\d{4,8})\b(?![.\d])")
LOGIN_PAIR = re.compile(
    r"(?i)\b((?:login|log in with|sign in with|creds|username\s*/\s*password|user\s*/\s*pass)(?:\s*(?:is|are|:|=))?\s+"
    r"|credentials?\s*(?:are|is|:|=)\s*)([^\s/:\-][^\s/:]{1,63})(\s*[/:]\s*)([^\s,;]{3,64})")
BASIC_AUTH = re.compile(r"(?i)(\bAuthorization:\s*Basic\s+)([A-Za-z0-9+/=]{8,})")
PHRASE = re.compile(r"(?i)\b((?:passphrase|pass phrase|seed phrase|recovery phrase|mnemonic)\s*(?:is|:|=)\s*)([^\n]{8,200})")
CODES = re.compile(r"(?i)\b((?:recovery|backup|2fa|mfa|one[- ]time|emergency)\s+codes?\s*(?:are|is|:|=)?\s*)([^\n]{4,300})")
URL_SECRET_PARAM = re.compile(
    r"(?i)([?&;](?:access_token|id_token|refresh_token|token|api_?key|key|secret|client_secret|sig|signature|"
    r"password|pwd|pass|auth|code|session|sid|x-amz-signature|x-amz-credential|x-amz-security-token|"
    r"awsaccesskeyid|x-goog-signature|x-goog-credential|sv|se|sp|skoid|sktid)=)([^&\s#\"'<>]+)")

URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@")
HIGH_ENTROPY = re.compile(r"(?<![A-Za-z0-9+_=\-/.])[A-Za-z0-9+_=\-]{32,}(?![A-Za-z0-9+_=\-/])")

EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<![\w.])(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\w.])")
# International numbers (+ and a country code) anywhere; national formats that are distinctive on
# their own (UK landlines in brackets, UK and Australian mobiles, French numbers); anything else
# only after a label such as "call", "phone" or "mobile".
DATE_LIKE = re.compile(r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b|"
                       r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?:19|20)?\d{2}\b")
TEL_LINK = re.compile(r"(?i)\btel:\+?[\d\-\s().]{7,}\d")
INTL_PHONE = re.compile(
    r"(?<![\w+/=.:-])\+[1-9]\d{0,2}(?:[ .\-]?\(?\d{1,5}\)?){1,5}[ .\-]?\d{2,5}(?![\w:]|\.\d)")
NATIONAL_PHONE = re.compile(
    r"(?<![\w.])(?:\(0\d{2,4}\)[ ]?\d{3,4}[ \-]?\d{3,4}|07\d{3}[ ]?\d{3}[ ]?\d{3}|04\d{2}[ ]?\d{3}[ ]?\d{3}|"
    r"0[1-9](?:[ .]\d{2}){4})(?![\w.])")
LABELLED_PHONE = re.compile(
    r"(?i)\b((?:call|phone|tel|telephone|mobile|cell|fax|whatsapp|signal|text|sms|landline|direct line|switchboard)"
    r"(?:[ \t]*(?:number|no\.?|#))?[ \t]*(?:me[ \t]+)?(?:at|on|is|:)?[ \t]*)(\(?\+?\d[\d \t().\-]{5,18}\d)")
_TLD = r"(?:com|org|net|edu|gov|mil|int|io|co|uk|ca|de|fr|au|nz|us|eu|info|me|dev|ai|app)"
_AT_BRACKET = r"\s*[\[({]\s*at\s*[\])}]\s*"
_DOT_ANY = r"(?:\s*[\[({]\s*dot\s*[\])}]\s*|\s+dot\s+|\.)"
_DOT_OBF = r"(?:\s*[\[({]\s*dot\s*[\])}]\s*|\s+dot\s+)"
OBFUSCATED_EMAIL = re.compile(
    r"(?i)\b[A-Za-z0-9._%+\-]+(?:" + _AT_BRACKET + r"[A-Za-z0-9\-]+(?:" + _DOT_ANY + r"[A-Za-z0-9\-]+)*" + _DOT_ANY + _TLD
    + r"|\s+at\s+[A-Za-z0-9\-]+(?:" + _DOT_ANY + r"[A-Za-z0-9\-]+)*" + _DOT_OBF + _TLD + r")\b")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
HOME = re.compile(r"(/Users/|/home/)([^/\s`'\")]+)|([A-Za-z]:\\\\?Users\\\\?)([^\\\s`'\")]+)")
INTERNAL_HOST = re.compile(
    r"(?<![\w@/.-])(?:[A-Za-z0-9-]+\.)+(?:local|internal|corp|lan|intranet|localdomain|private)(?![\w.-])", re.I
)
URL = re.compile(r"\bhttps?://[^\s)>\]\"'`]+")
_U = r"[^\s)>\]\"'`]*"
INTERNAL_SERVICES: list[tuple[str, re.Pattern]] = [(kind, re.compile(pat, re.I)) for kind, pat in [
    ("slack", r"https?://(?:[\w-]+\.)?slack\.com/(?:archives|client|messages|app_redirect|team|files|docs|canvas)/" + _U),
    ("slack", r"https?://[\w-]+\.(?:enterprise\.)?slack\.com\b" + _U),
    ("slack", r"slack://" + _U),
    ("jira", r"https?://[\w.-]+/(?:browse|jira)/" + _U),
    ("atlassian", r"https?://[\w-]+\.atlassian\.net/" + _U),
    ("confluence", r"https?://[\w.-]+/(?:confluence|display)/" + _U),
    ("sharepoint", r"https?://[\w-]+(?:-my)?\.sharepoint\.com/" + _U),
    ("onedrive", r"https?://(?:1drv\.ms|onedrive\.live\.com)/" + _U),
    ("google-doc", r"https?://(?:docs|drive|sheets|slides|forms)\.google\.com/" + _U),
    ("meeting", r"https?://(?:[\w-]+\.)?zoom\.us/(?:j|my|rec|w|s|wc)/" + _U),
    ("meeting", r"https?://teams\.microsoft\.com/l/" + _U),
    ("meeting", r"https?://[\w-]+\.webex\.com/" + _U),
    ("meeting", r"https?://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}\b" + _U),
    ("box", r"https?://(?:[\w-]+\.)?(?:app\.)?box\.com/(?:s|file|folder)/" + _U),
    ("dropbox", r"https?://(?:www\.)?dropbox\.com/(?:s|scl|sh)/" + _U),
    ("notion", r"https?://(?:www\.)?notion\.(?:so|site)/" + _U),
    ("figma", r"https?://(?:www\.)?figma\.com/(?:file|design|board|proto)/" + _U),
    ("miro", r"https?://miro\.com/app/board/" + _U),
    ("loom", r"https?://(?:www\.)?loom\.com/share/" + _U),
    ("s3", r"https?://[\w.-]+\.s3[\w.-]*\.amazonaws\.com/" + _U),
    ("s3", r"https?://s3[\w.-]*\.amazonaws\.com/[\w.-]+/" + _U),
]]
S3_URI = re.compile(r"\bs3://([\w.-]+)")
MEETING_ID = re.compile(r"(?i)\b((?:meeting|conference|webinar) id|access code|dial-in pin)(\s*[:#]?\s*)(\d[\d ]{6,14}\d)")
# Just before a word: the start of the text, a line, a list item or a sentence, optionally inside
# bold, a bracket or quotes.
SENTENCE_START = re.compile(r"(?:(?:^|\n)[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+)?|[.!?:;][ \t]+)(?:\*\*|__|\[|\(|\"|“)?$")
LOCAL_LINK = re.compile(r"(!?)\[([^\]]*)\]\((?!https?://|mailto:|#)([^)\s]+(?:\s+\"[^\"]*\")?)\)")
WIKILINK = re.compile(r"(!?)\[\[([^\]|#^]+)([#^][^\]|]*)?(?:\|([^\]]*))?\]\]")

SENSITIVE_WORDS = re.compile(
    r"(?i)\b(confidential|do not share|don't share|internal only|resume|résumé|cv\b|salary|salaries|compensation|"
    r"promotion|performance review|disciplinary|termination|layoffs?|severance|medical|"
    r"ssn|social security|passport number|date of birth|home address|applicants?|"
    r"interview(?:s|ed|ing)?|hiring decision|reference check|background check|nda|lawsuit|litigation)\b"
)

# Special categories of personal data. Any match holds a learning back, whatever its topic, because
# even with names removed the context can identify someone. Patterns are deliberately specific so
# that tech phrases ("state transition", "CSS transition", "race condition", "Visa card") don't fire.
SPECIAL_CATEGORIES: list[tuple[str, re.Pattern]] = [
    ("gender history", re.compile(
        r"(?i)\b(dead[- ]?names?|dead[- ]?naming|birth names?|name at birth|n[ée]e\b|formerly known as|f/k/a|"
        r"(?:my|her|his|their|your) (?:previous|former|old|birth|legal|real) (?:legal )?name|legal name change|"
        r"name change (?:petition|order|documents?)|changed (?:my|her|his|their) (?:legal )?name|"
        r"assigned (?:male|female) at birth|[AM]FAB|(?:I|she|he|they|we) transitioned|(?:I'm|I am|she's|she is|he's|he is|they're|they are) transitioning|"
        r"(?:gender|medical|social) transition|gender[- ]affirming|gender identity|gender dysphoria|transgender|"
        r"trans (?:woman|man|women|men|person|people|kid|youth)|non-?binary|genderqueer|misgender\w*|HRT|"
        r"hormone (?:therapy|replacement)|(?:top|bottom) surgery|(?:coming|came) out (?:as|to)\b)\b")),
    ("sexual orientation", re.compile(
        r"(?i)\b(sexual orientation|gay|lesbian|bisexual|pansexual|asexual|queer|LGBTQ\w*\+?)\b")),
    ("health", re.compile(
        r"(?i)\b(mental health|psychiatr(?:ist|ic|y)|therapist|therapy (?:session|appointment)s?|counseling|"
        r"depression|anxiety disorder|panic attacks?|PTSD|bipolar|ADHD|autis(?:m|tic)|disabilit(?:y|ies)|"
        r"chronic (?:illness|pain)|diagnosed with|prescriptions?|medications?|surgery|HIV|cancer|"
        r"pregnan(?:t|cy)|miscarriage)\b")),
    ("religion", re.compile(r"(?i)\b(religio(?:n|us)|church|mosque|synagogue|temple|my faith|atheis[mt])\b")),
    ("ethnicity", re.compile(r"(?i)\b(ethnicity|ethnic|racial|race and ethnicity)\b")),
    ("immigration", re.compile(
        r"(?i)\b(immigration status|visa status|green card|work permit|undocumented|asylum|deport\w*|citizenship status)\b")),
    ("criminal record", re.compile(
        r"(?i)\b(criminal (?:record|history|charges?)|arrest(?:ed)?|convict(?:ed|ion)|felony|misdemeanou?r|"
        r"probation|parole|incarcerat\w+)\b")),
    ("family and relationships", re.compile(
        r"(?i)\b(divorce|custody|separation agreement|restraining order|domestic violence)\b")),
]


AI_SOURCES = {"claude-memory", "claude-md", "agents-md", "gemini-md", "conventions-md", "cursor-rules", "windsurf-rules",
              "cline-rules", "copilot-instructions", "claude-skill", "claude-command", "claude-agent"}


def special_categories(text: str) -> list[str]:
    return [name for name, pat in SPECIAL_CATEGORIES if pat.search(text)]


KEEP_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
INTERNAL_SUFFIXES = (".local", ".internal", ".corp", ".lan", ".intranet", ".home", ".localdomain", ".private")


def entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


@dataclass
class Redactor:
    terms: dict[str, str] = field(default_factory=dict)  # term -> replacement
    people: set[str] = field(default_factory=set)  # names from the vault's People folder
    allow_domains: list[str] = field(default_factory=list)
    former: object = None  # kungfu.former_names.FormerNames; matches become "the user"
    cased_terms: dict[str, str] = field(default_factory=dict)  # matched only with this exact capitalization

    def __post_init__(self) -> None:
        pairs = [(t, r) for t, r in self.terms.items() if t.strip()]
        pairs += [(p, "[person]") for p in self.people if len(p) >= 3]
        # Longest first so "Jane Doe" wins over "Jane".
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        self._repl = {t.lower(): r for t, r in pairs}
        self._term_re = (
            # Letters/digits only as boundaries, so names inside identifiers (ACME_API_URL, ssh-acme-prod) match too.
            re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(t) for t, _ in pairs) + r")(?![A-Za-z0-9])", re.I)
            if pairs else None
        )
        self._people_lower = {p.lower() for p in self.people}
        cased = sorted((t for t in self.cased_terms if t.strip()), key=len, reverse=True)
        self._cased_re = (re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(t) for t in cased) + r")(?![A-Za-z0-9])")
                          if cased else None)

    # --- individual passes -------------------------------------------------------------------

    def secrets(self, text: str, found: list[str]) -> str:
        for name, pat in SECRET_PATTERNS:
            if pat.search(text):
                found.append(name)
                text = pat.sub(f"[REDACTED:{name}]", text)

        def _cli(m: re.Match) -> str:
            raw = m.group(2)
            v = raw.strip("'\"")
            if not v or PLACEHOLDER.match(v) or v.lower() in NOT_A_VALUE or v.startswith("-") or v == "stdin":
                return m.group(0)
            if raw == v and v.isalpha() and v.islower():
                return m.group(0)  # "the --passphrase flag": prose about the option, not a value
            found.append("cli-secret")
            return m.group(1) + "[REDACTED:secret]"

        for pat in CLI_SECRETS + DEVICE_KEYS:
            text = pat.sub(_cli, text)

        def _directive(m: re.Match) -> str:
            raw = m.group(2)
            v = raw.strip("'\"")
            if (not v or PLACEHOLDER.match(v) or v.lower() in NOT_A_VALUE or "[REDACTED" in v
                    or v.startswith(("/", "./", "~/", "$", "%{"))):
                return m.group(0)
            in_config = m.group(1)[:1] in " \t{;" or raw != v
            if not in_config and v.isalpha():
                return m.group(0)  # a line of prose such as "secret sauce", not a config directive
            found.append("config-secret")
            return m.group(1) + "[REDACTED:secret]"

        text = CONFIG_DIRECTIVE.sub(_directive, text)
        text = BRACE_DIRECTIVE.sub(_directive, text)

        def _phrase(m: re.Match) -> str:
            found.append("secret-assignment")
            return m.group(1) + "[REDACTED:secret]"

        text = PHRASE.sub(_phrase, text)

        def _codes(m: re.Match) -> str:
            if not re.search(r"[A-Za-z0-9]{4}[- ]?[A-Za-z0-9]{3,}", m.group(2)) or not re.search(r"\d", m.group(2)):
                return m.group(0)
            found.append("recovery-codes")
            return m.group(1) + "[REDACTED:recovery-codes]"

        text = CODES.sub(_codes, text)
        text = DOCKER_AUTH.sub(lambda m: (found.append("docker-auth"), m.group(1) + "[REDACTED:secret]" + m.group(3))[1],
                               text)
        text = LABELLED_LICENSE.sub(lambda m: (found.append("license-key"), m.group(1) + "[REDACTED:license-key]")[1],
                                    text)
        text = BASIC_AUTH.sub(lambda m: (found.append("basic-auth"), m.group(1) + "[REDACTED:secret]")[1], text)

        def _login(m: re.Match) -> str:
            pw = m.group(4)
            if m.group(2).startswith("[") or PLACEHOLDER.match(pw) or pw.lower() in NOT_A_VALUE or pw.startswith(("/", "http")):
                return m.group(0)
            if not re.search(r"[\d\W_]", pw.rstrip(".")) or pw[:1].isupper() and pw[1:].isalpha():
                return m.group(0)  # "Credential Issuance / Verifiable Presentations" is prose
            found.append("login-credentials")
            return m.group(1) + m.group(2) + m.group(3) + "[REDACTED:secret]"

        text = LOGIN_PAIR.sub(_login, text)
        text = LABELLED_SECRET.sub(lambda m: self._labelled(m, found), text)
        text = PIN.sub(lambda m: (found.append("pin"), m.group(1) + "[REDACTED:pin]")[1], text)

        def _param(m: re.Match) -> str:
            v = m.group(2)
            if PLACEHOLDER.match(v) or v.startswith("{") or len(v) < 6 or v.lower() in NOT_A_VALUE:
                return m.group(0)
            found.append("url-secret")
            return m.group(1) + "[REDACTED]"

        text = URL_SECRET_PARAM.sub(_param, text)
        if URL_CREDENTIALS.search(text):
            found.append("url-credentials")
            text = URL_CREDENTIALS.sub(r"\1[REDACTED:credentials]@", text)

        def _entropy(m: re.Match) -> str:
            tok = m.group(0)
            if re.fullmatch(r"[0-9a-f]+", tok) or re.fullmatch(r"[a-z0-9_\-]+", tok):
                return tok  # hashes, digests and slugs are not secrets
            classes = sum(bool(re.search(p, tok)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
            if classes == 3 and entropy(tok) > 4.2:
                found.append("high-entropy-string")
                return "[REDACTED:possible-secret]"
            return tok

        return HIGH_ENTROPY.sub(_entropy, text)

    def term_hits(self, text: str) -> Counter:
        """(term as configured, replacement) -> how many times it matches in text."""
        hits: Counter = Counter()
        canon = {t.lower(): t for t in self.terms}
        if self._term_re:
            for m in self._term_re.finditer(text):
                key = m.group(1).lower()
                hits[(canon.get(key, m.group(1)), self._repl[key])] += 1
        if self._cased_re:
            for m in self._cased_re.finditer(text):
                hits[(m.group(1), self.cased_terms[m.group(1)])] += 1
        return hits

    def clean_tags(self, tags: list[str]) -> list[str]:
        """Tags become topics, which travel in the bundle's manifest and the recipient's tags. A tag that
        names something you scrub (#acme-merger, #jane-doe-review) is dropped, not rewritten."""
        kept = []
        for tag in tags:
            words = re.sub(r"[-_/]+", " ", tag)
            cleaned, secrets, flags = self.clean_text(words)
            if cleaned == words and not secrets:
                kept.append(tag)
        return kept

    @staticmethod
    def public_keys(text: str, flags: list[str]) -> str:
        """Public keys and key fingerprints. They aren't secret, but they tie the text to a person's key:
        a PGP public key block carries names and email addresses (held back), and SSH keys and
        fingerprints are replaced without holding the learning back."""
        if PUBLIC_KEY_BLOCK.search(text):
            flags.append("id:pgp-public-key")
            text = PUBLIC_KEY_BLOCK.sub("[pgp-public-key]", text)
        if SSH_PUBLIC_KEY.search(text):
            flags.append("ssh-public-key")
            text = SSH_PUBLIC_KEY.sub(lambda m: m.group(1) + "[ssh-public-key]", text)

        def _fp(m: re.Match) -> str:
            flags.append("key-fingerprint")
            return (m.group(1) if m.lastindex else "") + "[key-fingerprint]"

        for pat in KEY_FINGERPRINT:
            text = pat.sub(_fp, text)
        return text

    @staticmethod
    def _labelled(m: re.Match, found: list[str]) -> str:
        """Decide whether the value after a secret-ish label is a real value, and replace it if so."""
        ident, label, suffix, sep = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        quoted, bare = m.group(5), m.group(6)
        value = (quoted[1:-1] if quoted else bare) or ""
        strong = re.fullmatch(STRONG_LABELS, label) is not None or (
            label in ("credential", "credentials", "storepass", "keypass") and ident.lower() != label)
        spaced = not re.search(r"[:=]", sep)  # "password is x", "secret x"
        low = value.lower().strip()
        if not value or "[REDACTED" in value or PLACEHOLDER.match(value) or low in NOT_A_VALUE or value[0] in "*_`#>|":
            return m.group(0)
        if label in ("pass", "pw", "pwd") and not re.search(r"\d", value):
            return m.group(0)  # "first pass: tidy the docs" is prose; "pw: Tr0ub4dor&3" isn't
        if value.startswith(("/", "./", "~/", "-", "http://", "https://")) or re.search(r"[(){}]|environ|getenv|process\.env|secrets\.", value):
            return m.group(0)  # a path, a flag, a URL or code that fetches the secret: not the secret
        if spaced:
            # "the password is hunter2" and "RADIUS secret testing123" count; "password manager",
            # "secret sauce", "Secrets Manager" and "pass the test" don't.
            if not strong or label in ("pass", "pw", "pwd") or value.isalpha():
                return m.group(0)
        last = re.split(r"[_.\-]|(?<=[a-z0-9])(?=[A-Z])", suffix)[-1].lower() if suffix else ""
        if last in DESCRIPTORS or (suffix and (value.isdigit() or low in ("true", "false"))):
            return m.group(0)  # secret_name: prod/db, password_min_length: 12, tokenTtl: 3600
        if not strong:
            # Generic token labels need something that looks generated: 8+ characters with a digit,
            # or 16+ with mixed case.
            if not (len(value) >= 8 and re.search(r"\d", value)) and not (
                    len(value) >= 16 and re.search(r"[a-z]", value) and re.search(r"[A-Z]", value)):
                return m.group(0)
        elif value.isalpha() and value.islower() and not (quoted or re.search(r"[:=]", sep)):
            return m.group(0)
        found.append("secret-assignment")
        replacement = "[REDACTED:secret]"
        if quoted:
            replacement = quoted[0] + replacement + quoted[0]
        return ident + sep + replacement

    def personal(self, text: str, flags: list[str]) -> str:
        # Former names first, before anything else rewrites the text around them.
        if self.former:
            text, n = self.former.scrub(text)
            if n:
                flags.append("former-name")
        if EMAIL.search(text) or OBFUSCATED_EMAIL.search(text):
            flags.append("email")
            text = EMAIL.sub("[email]", text)
            text = OBFUSCATED_EMAIL.sub("[email]", text)

        def _phone(m: re.Match, prefix: str = "") -> str:
            raw = m.group(m.lastindex or 0)
            digits = re.sub(r"\D", "", raw)
            if not 7 <= len(digits) <= 15 or DATE_LIKE.search(raw):
                return m.group(0)
            flags.append("phone")
            return prefix + "[phone]"

        text = TEL_LINK.sub(lambda m: (flags.append("phone"), "[phone]")[1], text)
        text = LABELLED_PHONE.sub(lambda m: _phone(m, m.group(1)), text)
        text = INTL_PHONE.sub(lambda m: _phone(m) if len(re.sub(r"\D", "", m.group(0))) >= 9 else m.group(0), text)
        text = NATIONAL_PHONE.sub(_phone, text)
        if PHONE.search(text):
            flags.append("phone")
            text = PHONE.sub("[phone]", text)

        def _ip(m: re.Match) -> str:
            s = m.group(0)
            try:
                ip = ipaddress.ip_address(s)
            except ValueError:
                return s
            if s in KEEP_IPS or any(ip in n for n in DOC_NETS):
                return s
            flags.append("ip-address")
            return "[ip]"

        text = IPV4.sub(_ip, text)

        def _home(m: re.Match) -> str:
            flags.append("home-path")
            return "~"

        text = HOME.sub(_home, text)

        def _url(m: re.Match) -> str:
            url = m.group(0)
            if re.match(r"https?://[^/]*[<{$\[]", url):
                return url  # a template such as https://<your-mdq>/entities/{id}: a placeholder, not a host
            try:
                host = (urlsplit(url).hostname or "").lower()
            except ValueError:
                return url
            if not host:
                return url
            if self._internal_host(host):
                flags.append("internal-url")
                return "[internal-url]"
            if not any(host == d or host.endswith("." + d) for d in self.allow_domains):
                flags.append(f"url:{host}")
            return url

        # Links into Slack, Jira, Confluence, SharePoint, Google Docs, meetings and buckets point at your
        # organization's private spaces: replaced, without holding the learning back.
        for kind, pat in INTERNAL_SERVICES:
            if pat.search(text):
                flags.append(f"internal-link:{kind}")
                text = pat.sub(f"[{kind}-link]", text)
        if S3_URI.search(text):
            flags.append("internal-link:s3")
            text = S3_URI.sub("s3://[bucket]", text)
        if MEETING_ID.search(text):
            flags.append("internal-link:meeting")
            text = MEETING_ID.sub(lambda m: m.group(1) + m.group(2) + "[meeting-id]", text)
        text = URL.sub(_url, text)
        if INTERNAL_HOST.search(text):
            flags.append("internal-host")
            text = INTERNAL_HOST.sub("[internal-host]", text)

        def _link(m: re.Match) -> str:
            embed, target, alias = m.group(1), m.group(2).strip(), (m.group(4) or "").strip()
            name = target.rsplit("/", 1)[-1]
            if embed:
                # An embedded file or note: its path and name say things (a folder of résumés, a
                # person's name), and it won't exist in the recipient's vault anyway.
                flags.append("attachment-link")
                return "[attachment]"
            if name.lower() in self._people_lower or target.lower() in self._people_lower:
                flags.append("person-link")
                return "[person]"
            # A link to another note becomes plain text: the link would be dead in the recipient's
            # vault, and the folder path in it isn't needed. The text is scrubbed like any other.
            flags.append("note-link")
            return alias or name

        text = WIKILINK.sub(_link, text)
        # Markdown links and images that point at local files keep their text and lose the path.
        text = LOCAL_LINK.sub(lambda m: (flags.append("attachment-link"),
                                         "[attachment]" if m.group(1) else (m.group(2) or "[attachment]"))[1], text)

        if self._cased_re:
            def _cased(m: re.Match) -> str:
                # "Rank unmigrated repos…", "Green builds only…": at the start of a sentence or list item and
                # followed by a lowercase word, a name that's also a word is far more likely the word.
                before = text[max(0, m.start() - 12):m.start()]
                after = text[m.end():m.end() + 6]
                if SENTENCE_START.search(before) and re.match(r"(?:\*\*|__|\]|\))? [a-z]", after):
                    return m.group(0)
                flags.append("redacted-term")
                return self.cased_terms[m.group(1)]

            text = self._cased_re.sub(_cased, text)
        if self._term_re:
            def _term(m: re.Match) -> str:
                flags.append("redacted-term")
                return self._repl[m.group(1).lower()]

            text = self._term_re.sub(_term, text)
        return text

    def _internal_host(self, host: str) -> bool:
        if host in ("localhost", "127.0.0.1"):
            return False
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_private
        except ValueError:
            pass
        return "." not in host or host.endswith(INTERNAL_SUFFIXES)

    # --- whole-learning pass -----------------------------------------------------------------

    def clean_text(self, text: str) -> tuple[str, list[str], list[str]]:
        """Return (clean text, secret kinds found, other flags)."""
        secrets: list[str] = []
        flags: list[str] = []
        text = self.public_keys(text, flags)  # before the secret scan, so they aren't called secrets
        text = self.secrets(text, secrets)
        # Identifiers before contact details, so an SSN or card number isn't half-eaten as a phone number.
        text, personal_ids, org_ids = identifiers.scrub(text)
        flags += [f"id:{k}" for k in personal_ids] + org_ids
        text = self.personal(text, flags)
        return text, secrets, flags

    def apply(self, item: Learning) -> Learning:
        title, s1, f1 = self.clean_text(item.title)
        summary, s2, f2 = self.clean_text(item.summary)
        body, s3, f3 = self.clean_text(item.body)
        item.title, item.summary, item.body = title, summary, body
        for f in dict.fromkeys(s1 + s2 + s3):
            item.flag(f"secret:{f}")
        for f in dict.fromkeys(f1 + f2 + f3):
            item.flag(f)
        if s1 or s2 or s3:
            item.held = item.held or "contained a secret (redacted); check it before sharing"
        ai_notes = item.source_type in AI_SOURCES
        findings = content.review(f"{title}\n{summary}\n{body}", ai_notes=ai_notes)
        for cat, detail in findings:
            item.flag(f"{cat}:{detail}")
        if findings:
            item.held = item.held or ("held back: " + "; ".join(d for _, d in findings))
        ids = [f[3:] for f in item.flags if f.startswith("id:")]
        if ids:
            item.held = item.held or (f"contained a personal identifier ({', '.join(ids)}), now removed; "
                                      "held back so you can check what's left")
        if "former-name" in item.flags:
            # Scrubbed, but the rest of the text may still identify the person: never offered by default.
            item.held = "mentioned a former name (scrubbed); held back so the context can't identify you"
        special = special_categories(f"{title}\n{summary}\n{body}")
        for cat in special:
            item.flag(f"special:{cat}")
        if special:
            item.held = item.held or f"sensitive personal subject ({', '.join(special)})"
        words = sorted({m.group(1).lower() for m in SENSITIVE_WORDS.finditer(f"{title}\n{summary}\n{body}")})
        if words:
            item.flag("sensitive:" + ",".join(words))
            item.held = item.held or f"sensitive subject ({', '.join(words)})"
        return item


def residual_secrets(text: str) -> list[str]:
    """Secrets and personal identifiers still present in final text (e.g. typed back in during review).
    Used as a hard gate at pack time."""
    found: list[str] = []
    Redactor().secrets(text, found)
    return sorted(set(found) | set(identifiers.residual(text)))


INJECTION = re.compile(
    r"(?i)(ignore (?:all |any )?(?:previous|prior|above) (?:instructions|rules)|disregard (?:the|your) "
    r"(?:system|previous)|you are now|new instructions:|system prompt|exfiltrat|send (?:the|your) "
    r"(?:keys|credentials|secrets|tokens)|curl [^\n]*\|\s*(?:sh|bash))"
)


def injection_markers(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in INJECTION.finditer(text)})
