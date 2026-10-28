"""Output guardrails: synthetic data must never be presented as a sales or demand forecast."""

from __future__ import annotations

import re

DEMAND_PATTERNS = [
    r"\b\d{1,3}(?:\.\d+)?\s?%\s+(?:of\s+\w+\s+)?(?:would|will|are likely to|plan to)\s+(?:buy|purchase|pay|subscribe|convert|sign up)",
    r"\b(?:expected|projected|predicted|forecast(?:ed)?|estimated)\s+(?:sales|revenue|demand|conversions?|units|market share)",
    r"\b(?:sales|demand|revenue)\s+(?:forecast|projection|prediction|estimate)",
    r"\bmarket\s+(?:size|potential|demand)\b",
    r"\bconversion rate\s+(?:will|would|of about|of around)",
    r"\b(?:will|would)\s+sell\s+(?:well|\d)",
    r"\bunits?\s+sold\b",
    r"\bwillingness[- ]to[- ]pay\s+(?:is|of)\s+\$?\d",
]
_COMPILED = [re.compile(p, re.I) for p in DEMAND_PATTERNS]
REPLACEMENT = "[removed: synthetic personas cannot predict sales or demand]"


def contains_demand_prediction(text: str) -> bool:
    return any(p.search(text) for p in _COMPILED)


def strip_demand_predictions(text: str) -> str:
    """Remove every sentence that reads as a sales/demand prediction."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [REPLACEMENT if contains_demand_prediction(s) else s for s in sentences]
    out = " ".join(kept)
    return re.sub(rf"(?:{re.escape(REPLACEMENT)}\s*)+", REPLACEMENT + " ", out).strip()
