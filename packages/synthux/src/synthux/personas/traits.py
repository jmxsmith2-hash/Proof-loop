"""Derive trait estimates from (anonymized) source records.

Structured columns are used when present (age, device, tech comfort ...).
Free text is scanned with small phrase lexicons. Every estimate keeps the
snippet that supports it so personas stay traceable to real data.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from synthux.personas.ingest import SourceRecord

LEXICON: dict[str, dict[Any, list[str]]] = {
    "tech_skill": {
        1: ["not good with computers", "not very techy", "not tech", "my grandson", "my granddaughter",
            "my kids set", "computers confuse", "technophobe", "i struggle with apps", "don't understand apps"],
        2: ["not great with tech", "confusing", "couldn't figure", "could not figure", "hard to use", "too complicated"],
        4: ["comfortable with tech", "use lots of apps", "tech savvy", "early adopter"],
        5: ["developer", "engineer", "power user", "keyboard shortcut", "api", "i work in it", "programmer"],
    },
    "patience": {
        1: ["gave up", "give up", "waste of time", "no patience", "i left", "closed the app", "uninstalled", "rage"],
        2: ["too long", "too slow", "annoying", "frustrat", "took forever"],
        4: ["took a while but", "eventually", "after a few tries", "figured it out"],
        5: ["happy to explore", "read the manual", "don't mind spending time", "patient"],
    },
    "familiarity": {
        1: ["first time", "never used", "new to", "first online", "never bought online"],
        2: ["not used to", "unfamiliar"],
        4: ["used similar", "used before", "switched from"],
        5: ["for years", "every day", "daily user", "used it for years"],
    },
    "device": {
        "mobile": ["phone", "mobile", "iphone", "android", "on my cell", "smartphone"],
        "tablet": ["tablet", "ipad"],
        "desktop": ["laptop", "desktop", "pc", "computer at work", "mac"],
    },
    "budget": {
        "tight": ["expensive", "too pricey", "can't afford", "cheap", "on a budget", "overpriced", "price"],
        "generous": ["worth every penny", "premium", "don't mind paying", "price is fine"],
    },
    "eyesight": {
        "low_vision": ["small text", "can't read", "cannot read", "font size", "my eyesight", "magnif", "reading glasses", "too small to read"],
        "color_blind": ["colour blind", "color blind", "colorblind", "colourblind"],
    },
    "motor": {
        "tremor": ["tremor", "shaky hands", "parkinson"],
        "limited_dexterity": ["arthritis", "hard to tap", "fat fingers", "tiny buttons", "small buttons"],
    },
    "time_pressure": {
        True: ["in a hurry", "quickly", "lunch break", "on the go", "no time", "rushing", "between meetings", "on the bus"],
    },
    "mood": {
        "stressed": ["stressed", "anxious", "worried", "urgent"],
        "distracted": ["kids", "distracted", "multitask", "while cooking", "interrupted"],
    },
    "language": {
        "second_language": ["english is not my first", "my second language", "non-native", "translate"],
    },
}

COLUMN_SYNONYMS: dict[str, list[str]] = {
    "age_range": ["age", "age_range", "age_group", "agebracket", "age_bracket", "user_age_bracket"],
    "device": ["device", "devicecategory", "device_category", "platform", "device_used"],
    "tech_skill": ["tech_skill", "tech_comfort", "tech_savviness", "digital_skill", "comfort_with_technology", "tech"],
    "patience": ["patience"],
    "familiarity": ["familiarity", "experience", "used_similar", "prior_experience"],
    "budget": ["budget", "income", "spend", "budget_usd"],
    "goal": ["goal", "reason", "why", "task", "intent", "main_goal", "what_did_you_want_to_do"],
}

GOAL_PATTERN = re.compile(r"\b(?:i (?:want|wanted|need|needed|was trying|am trying|tried) to|looking for|trying to)\s+([^.!?\n]{4,80})", re.I)
WORD_SCALE = {"very low": 1, "low": 2, "medium": 3, "average": 3, "moderate": 3, "high": 4, "very high": 5,
              "beginner": 1, "novice": 2, "intermediate": 3, "advanced": 4, "expert": 5,
              "none": 1, "some": 3, "lots": 5, "never": 1, "sometimes": 3, "often": 4, "always": 5}


@dataclass
class TraitEvidence:
    values: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(list))
    quotes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    goals: list[str] = field(default_factory=list)
    weights: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    records: int = 0

    def add(self, trait: str, value: Any, quote: str = "", weight: float = 1.0) -> None:
        self.values[trait].append(value)
        self.weights[trait][value] += weight
        if quote and len(self.quotes[trait]) < 5:
            self.quotes[trait].append(quote)

    def distribution(self, trait: str) -> Counter:
        return self.weights.get(trait, Counter())


def _scale(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in WORD_SCALE:
        return WORD_SCALE[text]
    try:
        number = float(text)
    except ValueError:
        return None
    if 1 <= number <= 5:
        return int(round(number))
    if 0 <= number <= 10:
        return max(1, min(5, int(round(number / 2))))
    return None


def _age_range(value: Any) -> str | None:
    text = str(value).strip()
    if re.fullmatch(r"\d{2}\s*-\s*\d{2}|65\+|\d{2}\+", text):
        text = text.replace(" ", "")
        low = int(text[:2])
    else:
        try:
            low = int(float(text))
        except ValueError:
            return None
    for bound, label in ((24, "18-24"), (34, "25-34"), (44, "35-44"), (54, "45-54"), (64, "55-64")):
        if low <= bound:
            return label
    return "65+"


def _device(value: Any) -> str | None:
    text = str(value).lower()
    for device in ("mobile", "tablet", "desktop"):
        if device in text:
            return device
    if any(w in text for w in ("phone", "iphone", "android")):
        return "mobile"
    if any(w in text for w in ("ipad",)):
        return "tablet"
    if any(w in text for w in ("laptop", "pc", "mac", "computer", "web")):
        return "desktop"
    return None


def _from_columns(record: SourceRecord, ev: TraitEvidence) -> None:
    for trait, names in COLUMN_SYNONYMS.items():
        value = next((record.fields[n] for n in names if n in record.fields and record.fields[n] not in ("", None)), None)
        if value is None:
            continue
        if trait == "age_range" and (age := _age_range(value)):
            ev.add("age_range", age, weight=record.weight)
        elif trait == "device" and (dev := _device(value)):
            ev.add("device", dev, weight=record.weight)
        elif trait in ("tech_skill", "patience", "familiarity") and (n := _scale(value)):
            ev.add(trait, n, weight=record.weight)
        elif trait == "budget":
            number = _scale(value)
            if number:
                ev.add("budget", "tight" if number <= 2 else "generous" if number >= 4 else "moderate")
            elif str(value).lower() in ("tight", "moderate", "generous"):
                ev.add("budget", str(value).lower())
        elif trait == "goal" and isinstance(value, str) and len(value.split()) >= 2:
            ev.goals.append(value.strip()[:120])


def _sentence_around(text: str, index: int) -> str:
    start = max(text.rfind(".", 0, index), text.rfind("!", 0, index), text.rfind("?", 0, index)) + 1
    end_candidates = [i for i in (text.find(".", index), text.find("!", index), text.find("?", index)) if i != -1]
    end = min(end_candidates) + 1 if end_candidates else len(text)
    return text[start:end].strip()[:240]


def _from_text(record: SourceRecord, ev: TraitEvidence) -> None:
    text = record.text
    lower = text.lower()
    if not lower:
        return
    for trait, levels in LEXICON.items():
        for value, phrases in levels.items():
            for phrase in phrases:
                idx = lower.find(phrase)
                if idx != -1 and (phrase != "api" or re.search(r"\bapi\b", lower)):
                    ev.add(trait, value, _sentence_around(text, idx))
                    break
    for match in GOAL_PATTERN.finditer(text):
        ev.goals.append(match.group(1).strip().rstrip(",;"))


def derive_evidence(records: list[SourceRecord]) -> TraitEvidence:
    ev = TraitEvidence()
    for record in records:
        ev.records += 1
        _from_columns(record, ev)
        _from_text(record, ev)
    return ev


def record_traits(record: SourceRecord) -> dict[str, Any]:
    """Trait estimates for a single record (used to build data-grounded anchors)."""
    ev = derive_evidence([record])
    out: dict[str, Any] = {}
    for trait, counter in ev.weights.items():
        if counter:
            out[trait] = counter.most_common(1)[0][0]
    if ev.goals:
        out["goal"] = ev.goals[0]
    return out
