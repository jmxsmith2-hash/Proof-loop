"""Guardrails for physical-product output.

* Every output is labelled as a prediction, not a test.
* No claims about feel, perceived weight, texture, taste or smell. Comparing a
  *spec value* (e.g. 520 g) with similar products is allowed; saying it
  "feels heavy" is not. Quotes from real reviews of other products are evidence
  and are kept verbatim, clearly attributed.
"""

from __future__ import annotations

import re

LABEL = "Prediction, not a test"
SENSORY = re.compile(
    r"\b(?:feels?|feeling|felt|heavy in (?:the )?hand|light in (?:the )?hand|texture[sd]?|textured|to the touch|"
    r"soft(?:ness)?|rough(?:ness)?|silky|smooth(?:ness)? (?:to|in)|tastes?|tasted|tasting|flavou?r|smells?|"
    r"smelled|odou?rs?|aroma|comfortable (?:to hold|in (?:the )?hand)|grip feels|weighty|heft)\b",
    re.I,
)


def sensory_claim(text: str) -> str | None:
    match = SENSORY.search(text)
    return match.group(0) if match else None


def label(text: str) -> str:
    return text if text.startswith(LABEL) else f"{LABEL}: {text}"
