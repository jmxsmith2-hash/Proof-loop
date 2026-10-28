"""Turn reviews of similar products into a complaint taxonomy (with real quotes)."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from synthux.personas.anonymize import anonymize_text

# Only phrases that are complaints on their own; neutral words ("dishwasher", "cup holder") are left out
# because they appear in praise just as often.
CATEGORIES: dict[str, list[str]] = {
    "size": ["too big", "too large", "too small", "bulky", "doesn't fit", "does not fit", "won't fit",
             "too tall", "too short", "too wide", "too narrow", "huge", "tiny"],
    "weight": ["too heavy", "heavy", "weighs a ton", "too light", "flimsy and light"],
    "durability": ["broke", "broken", "cracked", "snapped", "fell apart", "dented", "worn out", "stopped working",
                   "rust", "peeling", "after a month", "after two weeks", "fragile", "flimsy"],
    "ergonomics": ["hard to open", "hard to hold", "slippery", "awkward", "uncomfortable", "hurts", "hand cramp",
                   "difficult to grip", "hard to carry", "lid is stiff", "can't open", "cannot open"],
    "leaks": ["leak", "leaks", "leaked", "leaking", "spill", "spilled", "drips"],
    "cleaning": ["hard to clean", "can't clean", "mold", "mould", "not dishwasher safe", "narrow mouth", "smells after"],
    "value": ["overpriced", "too expensive", "not worth", "waste of money", "cheaper", "pricey", "rip off"],
    "performance": ["doesn't keep", "does not keep", "lukewarm", "not cold", "not hot", "loses heat", "stops working",
                    "battery died", "battery drains", "weak"],
    "instructions": ["no instructions", "confusing instructions", "manual is useless", "hard to set up",
                     "hard to assemble"],
}
NEGATIONS = ("not ", "never ", "no ", "isn't ", "wasn't ", "doesn't ", "didn't ")
POSITIVE_OVERRIDE = re.compile(r"\b(?:not|never|no)\s+(?:too\s+)?(?:heavy|big|bulky|leak|leaks|broke)\b", re.I)


@dataclass
class Complaint:
    category: str
    mentions: int
    share: float
    quotes: list[str] = field(default_factory=list)
    phrases: dict[str, int] = field(default_factory=dict)
    products: dict[str, int] = field(default_factory=dict)  # product name -> mentions


@dataclass
class Review:
    text: str
    product: str = ""
    rating: float | None = None


def load_reviews(paths: list[Path]) -> list[Review]:
    reviews: list[Review] = []
    for path in paths:
        if path.suffix.lower() == ".csv":
            with path.open(newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    text = row.get("review") or row.get("text") or row.get("review_text") or ""
                    rating = row.get("rating") or row.get("stars")
                    reviews.append(Review(anonymize_text(text), row.get("product", ""),
                                          float(rating) if rating not in (None, "") else None))
        else:
            for chunk in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
                if chunk.strip():
                    reviews.append(Review(anonymize_text(chunk.strip())))
    return [r for r in reviews if r.text]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _mentions(sentence: str, phrase: str) -> bool:
    lower = sentence.lower()
    idx = lower.find(phrase)
    if idx == -1:
        return False
    before = lower[max(0, idx - 12): idx]
    return not any(before.endswith(n) or n in before for n in NEGATIONS) and not POSITIVE_OVERRIDE.search(sentence)


def complaint_taxonomy(reviews: list[Review]) -> list[Complaint]:
    """Count complaints per category (each review counts once per category)."""
    found: dict[str, Complaint] = {}
    for review in reviews:
        hit_categories: set[str] = set()
        for sentence in _sentences(review.text):
            for category, phrases in CATEGORIES.items():
                for phrase in phrases:
                    if _mentions(sentence, phrase):
                        c = found.setdefault(category, Complaint(category, 0, 0.0))
                        c.phrases[phrase] = c.phrases.get(phrase, 0) + 1
                        if category not in hit_categories:
                            hit_categories.add(category)
                            c.mentions += 1
                            if review.product:
                                c.products[review.product] = c.products.get(review.product, 0) + 1
                            if len(c.quotes) < 4:
                                c.quotes.append(sentence[:200])
                        break
    total = max(1, len(reviews))
    for c in found.values():
        c.share = round(c.mentions / total, 3)
    return sorted(found.values(), key=lambda c: -c.mentions)
