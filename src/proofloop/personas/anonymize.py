"""Strip personal data from source material before any model or report sees it.

Removes email addresses, phone numbers, street addresses, postcodes, card-like
numbers, social handles, IP addresses and person names. Errs on the side of
removing too much.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_WITH_USER = re.compile(r"https?://\S*(?:user|profile|u/|@)\S*", re.I)
PHONE = re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3,4}[\s.-]\d{3,4}(?:[\s.-]\d{2,4})?(?!\w)")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
IPV4 = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{2,30}\b")
STREET_SUFFIX = (r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|way|place|pl|"
                 r"terrace|close|crescent|square|sq|highway|hwy|parkway|pkwy)")
ADDRESS = re.compile(
    rf"\b\d{{1,6}}\s+(?:[A-Z][a-z]+\.?\s+){{1,4}}{STREET_SUFFIX}\b\.?(?:,?\s*(?:apt|unit|suite|#)\s*\w+)?",
    re.I,
)
UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")
US_ZIP_STATE = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
NAME_INTRO = re.compile(
    r"\b(my name is|i am|i'm|this is|call me|signed|regards|thanks|thank you|cheers|best|from|by|hi|hello|dear)"
    r"([,:]?\s+)((?:[A-Z][a-z'-]+)(?:\s+[A-Z][a-z'-]+){0,2})",
    re.I,
)
CAPITALIZED = re.compile(r"\b[A-Z][a-z'-]{1,}\b")
AMBIGUOUS_NAMES = frozenset(
    "mark will grace rose bill dawn ruby frank jack joy art sue may dean lee li chen wei hope faith "
    "summer sky max penny sandy rob pat ray victor".split()
)
PII_COLUMNS = frozenset(
    "name full_name first_name last_name surname customer customer_name author reviewer username user "
    "email e-mail email_address phone phone_number mobile telephone address street address_line1 "
    "address_line2 city_address postcode zip zipcode ip ip_address".split()
)


@lru_cache(maxsize=1)
def first_names() -> frozenset[str]:
    text = (resources.files("proofloop") / "data" / "first_names.txt").read_text(encoding="utf-8")
    return frozenset(text.split())


@dataclass
class AnonymizationReport:
    replaced: dict[str, int] = field(default_factory=dict)

    def bump(self, kind: str, n: int = 1) -> None:
        if n:
            self.replaced[kind] = self.replaced.get(kind, 0) + n

    def merge(self, other: AnonymizationReport) -> None:
        for kind, n in other.replaced.items():
            self.bump(kind, n)

    @property
    def total(self) -> int:
        return sum(self.replaced.values())


def _sub(pattern: re.Pattern[str], token: str, text: str, report: AnonymizationReport, kind: str) -> str:
    text, n = pattern.subn(token, text)
    report.bump(kind, n)
    return text


def _replace_intro_names(text: str, report: AnonymizationReport) -> str:
    def repl(match: re.Match[str]) -> str:
        candidate = match.group(3)
        words = candidate.split()
        # "I'm Happy" should not become "[NAME]": require a known first name or a two-word capitalized name
        known = words[0].lower() in first_names()
        lead = match.group(1).lower()
        if known or (len(words) >= 2 and lead in ("my name is", "call me", "signed", "regards", "dear")):
            report.bump("name")
            return f"{match.group(1)}{match.group(2)}[NAME]"
        return match.group(0)
    return NAME_INTRO.sub(repl, text)


def _replace_known_names(text: str, report: AnonymizationReport) -> str:
    names = first_names()
    out: list[str] = []
    last = 0
    for match in CAPITALIZED.finditer(text):
        word = match.group(0)
        lower = word.lower()
        if lower not in names:
            continue
        before = text[: match.start()].rstrip()
        sentence_start = not before or before[-1] in ".!?\n:"
        if lower in AMBIGUOUS_NAMES and sentence_start:
            continue
        end = match.end()
        follow = re.match(r"\s+([A-Z][a-z'-]+)\b", text[end:])
        if follow:  # include a surname
            end += follow.end()
        out.append(text[last: match.start()])
        out.append("[NAME]")
        report.bump("name")
        last = end
    out.append(text[last:])
    return "".join(out)


def anonymize_text(text: str, report: AnonymizationReport | None = None) -> str:
    report = report if report is not None else AnonymizationReport()
    text = _sub(EMAIL, "[EMAIL]", text, report, "email")
    text = _sub(URL_WITH_USER, "[URL]", text, report, "url")
    text = _sub(IPV4, "[IP]", text, report, "ip")
    text = _sub(CARD, "[NUMBER]", text, report, "number")
    text = _sub(PHONE, "[PHONE]", text, report, "phone")
    text = _sub(ADDRESS, "[ADDRESS]", text, report, "address")
    text = _sub(UK_POSTCODE, "[POSTCODE]", text, report, "postcode")
    text = _sub(US_ZIP_STATE, "[POSTCODE]", text, report, "postcode")
    text = _sub(HANDLE, "[HANDLE]", text, report, "handle")
    text = _replace_intro_names(text, report)
    text = _replace_known_names(text, report)
    return text


def anonymize_record(record: dict[str, object], report: AnonymizationReport | None = None) -> dict[str, object]:
    """Drop PII columns entirely and scrub every remaining text value."""
    report = report if report is not None else AnonymizationReport()
    clean: dict[str, object] = {}
    for key, value in record.items():
        norm = str(key).strip().lower().replace(" ", "_")
        if norm in PII_COLUMNS:
            report.bump("column")
            continue
        clean[key] = anonymize_text(value, report) if isinstance(value, str) else value
    return clean
