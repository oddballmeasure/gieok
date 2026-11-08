"""Redaction at ingest, on the origin host, before anything is stored or replicated (guardrail 3).

Credentials become [secret:<kind>]; each hit is counted, and "live" kinds (keys that work against a real
account) are reported so Monty gets pinged: an agent already saw them. Customer PII becomes [redacted]
with no mapping kept, so nothing can be reversed.

Limits, stated plainly: free-text names and postal addresses can't be found by pattern. They are caught
only as values of known fields ("name", "address", ...) in JSON and API output. Replacements are fixed
strings, so redacted text still deduplicates."""

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REDACTED = "[redacted]"
# Optional backslashes: transcripts hold JSON inside JSON strings, where quotes are escaped.
Q = r'\\*"'

SECRETS: list[tuple[str, re.Pattern, bool]] = [  # (kind, pattern, live)
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), True),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), True),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"), True),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}"), True),
    ("stripe-test-key", re.compile(r"\b(?:sk|rk)_test_[A-Za-z0-9]{16,}"), False),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"), True),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), True),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), True),
    ("discord-token", re.compile(r"\b[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"), True),
    ("sentry-token", re.compile(r"\bsntry[su]_[A-Za-z0-9_=+/-]{40,}"), True),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), False),
    ("bearer", re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._~+/=-]{20,}"), False),
    ("cookie", re.compile(r"(?i)(?<=cookie: )[^\n\"\\]{8,}"), False),
    ("assigned-secret", re.compile(
        r"(?i)(?<![A-Za-z0-9_\[])((?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)"
        # A literal value only: not `$VAR`, `${...}`, `<placeholder>` or a call like `os.getenv(`.
        r"[\"'\\]*\s*[=:]\s*[\"'\\]*)(?![$<{%])([^\s\"'\\,;()\[\]{}<>]{8,})(?=[\s\"'\\,;]|$)"), False),
]

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<![\w.-])(?:\+\d{1,3}[ .-]?)?\(?\d{2,4}\)?[ .-]\d{3,4}[ .-]\d{3,4}(?![\w.-])")
IPV4 = re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b")
# Card numbers: an issuer prefix (Visa, Mastercard, Amex, Discover), grouped or 15-16 digits, then Luhn.
# Millisecond timestamps (17..., 18...) never match, so JSON numbers in transcripts stay intact.
# Anchored so decimals ("costUSD":0.4242...) and UUID pieces (42424242-...) never match: either would break JSON.
CARD = re.compile(r"(?<![\w.-])(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6011)(?:[ -]?\d{4}){2}[ -]?\d{3,4}(?![\w.-])")
STRIPE_CUSTOMER = re.compile(r"\b(?:cus|pm|card|ch|pi|sub|in)_[A-Za-z0-9]{10,}\b")
# Not "name": tool calls and frontmatter use it everywhere ("name": "Bash").
PII_KEYS = ("email", "full_name", "first_name", "last_name", "display_name", "phone",
            "address", "street", "city", "postcode", "postal_code", "zip", "user_id", "account_id",
            "discord_user_id", "customer", "customer_id", "ip", "ip_address")
PII_FIELD = re.compile(rf"({Q}(?:{'|'.join(PII_KEYS)}){Q}\s*:\s*{Q})([^\"\\]+)({Q})", re.I)


@dataclass
class Result:
    text: str
    secrets: Counter = field(default_factory=Counter)
    pii: Counter = field(default_factory=Counter)
    live: list[str] = field(default_factory=list)  # kinds of live-looking credentials found

    @property
    def count(self) -> int:
        return sum(self.secrets.values()) + sum(self.pii.values())


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def known_values(paths: list[Path]) -> list[str]:
    """Credential values from settings files the ingester can see, so they're caught in any form."""
    found = []
    name = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL|COOKIE", re.I)
    for path in paths:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.partition("=")
            value = value.strip().strip("\"'")
            if sep and name.search(key) and len(value) >= 8:
                found.append(value)
    return sorted(set(found), key=len, reverse=True)


def scrub(text: str, known: list[str] = ()) -> Result:
    r = Result(text)
    for value in known:
        if value in r.text:
            r.secrets["known-value"] += r.text.count(value)
            r.text = r.text.replace(value, "[secret:known-value]")
            r.live.append("known-value")
    for kind, pattern, live in SECRETS:
        def sub(m: re.Match, kind=kind) -> str:
            r.secrets[kind] += 1
            return f"{m.group(1)}[secret:{kind}]" if kind == "assigned-secret" else f"[secret:{kind}]"
        before = sum(r.secrets.values())
        r.text = pattern.sub(sub, r.text)
        if live and sum(r.secrets.values()) > before:
            r.live.append(kind)

    def pii(kind: str):
        def sub(m: re.Match) -> str:
            r.pii[kind] += 1
            return REDACTED
        return sub

    def field_sub(m: re.Match) -> str:
        if m.group(2) == REDACTED or m.group(2).startswith("[secret:"):
            return m.group(0)
        r.pii["field"] += 1
        return f"{m.group(1)}{REDACTED}{m.group(3)}"

    r.text = PII_FIELD.sub(field_sub, r.text)
    r.text = EMAIL.sub(pii("email"), r.text)
    r.text = STRIPE_CUSTOMER.sub(pii("payment-id"), r.text)

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if _luhn(digits) and len(set(digits)) > 1:
            r.pii["card"] += 1
            return REDACTED
        return m.group(0)

    r.text = CARD.sub(card, r.text)
    r.text = IPV4.sub(pii("ip"), r.text)
    r.text = PHONE.sub(pii("phone"), r.text)
    return r
