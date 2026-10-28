"""Deliberate spread of personas across the trait space.

1. Anchors come from the source data: records are grouped by their dominant
   traits, and the largest groups become personas first (grounding).
2. Remaining slots are filled by farthest-point sampling over a candidate grid,
   so each new persona is as different as possible from those already chosen.
3. Coverage rules make sure the extremes of skill and patience, a mobile user
   and (optionally) an accessibility persona are present.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any

import numpy as np

from synthux.personas.ingest import SourceRecord
from synthux.personas.models import (AGE_RANGES, BUDGETS, DATE_HABITS, DEVICES, MOODS, STYLES, Traits)
from synthux.personas.traits import record_traits

# Weights for the trait-vector dimensions (see Traits.vector): key behaviour drivers count more.
# Sensitive traits (eyesight, motor, language) are weighted low so that farthest-point sampling does not
# over-select them; the coverage rules add one accessibility persona explicitly.
DIM_WEIGHTS = np.array([2.0, 2.0, 1.5, 0.8, 1.2, 0.3, 0.3, 0.6, 0.8, 0.6, 0.25, 0.5, 0.9, 0.6])


def trait_distance(a: Traits, b: Traits) -> float:
    va, vb = np.array(a.vector()), np.array(b.vector())
    return float(np.sqrt(np.sum(DIM_WEIGHTS * (va - vb) ** 2)) / np.sqrt(DIM_WEIGHTS.sum()))


def trait_similarity(a: Traits, b: Traits) -> float:
    return 1.0 - trait_distance(a, b)


def _candidate_pool(rng: random.Random, size: int = 2500) -> list[Traits]:
    pool = []
    for _ in range(size):
        skill = rng.randint(1, 5)
        eyesight = rng.choices(["normal", "low_vision", "color_blind"], [0.84, 0.1, 0.06])[0]
        motor = rng.choices(["none", "tremor", "limited_dexterity"], [0.9, 0.05, 0.05])[0]
        pool.append(Traits(
            tech_skill=skill,
            patience=rng.randint(1, 5),
            familiarity=rng.randint(1, 5),
            age_range=rng.choice(AGE_RANGES),
            device=rng.choice(DEVICES),
            eyesight=eyesight,
            motor=motor,
            mood=rng.choice(MOODS),
            time_pressure=rng.random() < 0.35,
            budget=rng.choice(BUDGETS),
            language=rng.choices(["native", "second_language"], [0.85, 0.15])[0],
            date_habit=rng.choice(DATE_HABITS),
            style=rng.choice(STYLES),
            hand_size=rng.choices(["small", "medium", "large"], [0.3, 0.4, 0.3])[0],
            grip_strength="low" if motor != "none" else rng.choices(["low", "normal", "high"], [0.15, 0.65, 0.2])[0],
        ))
    return pool


def _anchor_from_group(group: list[dict[str, Any]], rng: random.Random) -> Traits:
    merged: dict[str, Any] = {}
    for key in {k for g in group for k in g}:
        values = [g[key] for g in group if key in g]
        merged[key] = Counter(values).most_common(1)[0][0]
    base = Traits(
        tech_skill=merged.get("tech_skill", 3),
        patience=merged.get("patience", 3),
        familiarity=merged.get("familiarity", 3),
        age_range=merged.get("age_range", rng.choice(AGE_RANGES[1:5])),
        device=merged.get("device", "desktop"),
        eyesight=merged.get("eyesight", "normal"),
        motor=merged.get("motor", "none"),
        mood=merged.get("mood", "calm"),
        time_pressure=bool(merged.get("time_pressure", False)),
        budget=merged.get("budget", "moderate"),
        language=merged.get("language", "native"),
        goal=merged.get("goal", "get the task done"),
        date_habit=rng.choice(DATE_HABITS),
        style=rng.choice(STYLES),
    )
    return base


def data_anchors(records: list[SourceRecord], limit: int, rng: random.Random) -> list[tuple[Traits, list[SourceRecord]]]:
    """Group records by their strongest traits; the biggest groups become anchors."""
    groups: dict[tuple, list[tuple[dict[str, Any], SourceRecord]]] = {}
    for record in records:
        traits = record_traits(record)
        if len([k for k in traits if k != "goal"]) < 1:
            continue
        key = (
            traits.get("device"),
            {1: "low", 2: "low", 3: "mid", 4: "high", 5: "high"}.get(traits.get("tech_skill"), None),
            {1: "low", 2: "low", 3: "mid", 4: "high", 5: "high"}.get(traits.get("patience"), None),
            traits.get("eyesight") not in (None, "normal") or traits.get("motor") not in (None, "none"),
        )
        groups.setdefault(key, []).append((traits, record))
    ranked = sorted(groups.values(), key=lambda g: -sum(r.weight for _, r in g))
    anchors = []
    for group in ranked[:limit]:
        anchors.append((_anchor_from_group([t for t, _ in group], rng), [r for _, r in group]))
    return anchors


def _farthest(pool: list[Traits], chosen: list[Traits], exclude: set[int]) -> int:
    best_idx, best_dist = -1, -1.0
    for i, candidate in enumerate(pool):
        if i in exclude:
            continue
        dist = min((trait_distance(candidate, c) for c in chosen), default=1.0)
        if dist > best_dist:
            best_idx, best_dist = i, dist
    return best_idx


def _coverage_missing(chosen: list[Traits], include_accessibility: bool) -> list[str]:
    missing = []
    skills = {t.tech_skill for t in chosen}
    patience = {t.patience for t in chosen}
    if not skills & {1, 2}:
        missing.append("low_skill")
    if not skills & {4, 5}:
        missing.append("high_skill")
    if not patience & {1, 2}:
        missing.append("low_patience")
    if not patience & {4, 5}:
        missing.append("high_patience")
    if not any(t.device == "mobile" for t in chosen):
        missing.append("mobile")
    if include_accessibility and not any(t.eyesight != "normal" or t.motor != "none" for t in chosen):
        missing.append("accessibility")
    return missing


def _satisfies(t: Traits, need: str) -> bool:
    return {
        "low_skill": t.tech_skill <= 2,
        "high_skill": t.tech_skill >= 4,
        "low_patience": t.patience <= 2,
        "high_patience": t.patience >= 4,
        "mobile": t.device == "mobile",
        "accessibility": t.eyesight != "normal" or t.motor != "none",
    }[need]


def design_traits(
    count: int,
    records: list[SourceRecord] | None = None,
    *,
    seed: int = 7,
    include_accessibility: bool = True,
    anchor_share: float = 0.5,
) -> list[tuple[Traits, list[SourceRecord]]]:
    """Choose `count` trait sets, grounded in data where possible and spread deliberately."""
    rng = random.Random(seed)
    records = records or []
    chosen: list[tuple[Traits, list[SourceRecord]]] = []
    max_anchors = max(0, int(round(count * anchor_share))) if records else 0
    chosen.extend(data_anchors(records, max_anchors, rng))
    pool = _candidate_pool(rng)
    used: set[int] = set()
    while len(chosen) < count:
        traits_so_far = [t for t, _ in chosen]
        missing = _coverage_missing(traits_so_far, include_accessibility and count >= 5)
        slots_left = count - len(chosen)
        if missing and slots_left <= len(missing) + 1:
            need = missing[0]
            subset = {i for i, t in enumerate(pool) if not _satisfies(t, need)} | used
            idx = _farthest(pool, traits_so_far, subset)
        else:
            idx = _farthest(pool, traits_so_far, used)
        used.add(idx)
        chosen.append((pool[idx], []))
    _assign_goals(chosen, records, rng)
    return chosen[:count]


def _assign_goals(chosen: list[tuple[Traits, list[SourceRecord]]], records: list[SourceRecord], rng: random.Random) -> None:
    from synthux.personas.traits import derive_evidence

    goals = derive_evidence(records).goals if records else []
    for i, (traits, recs) in enumerate(chosen):
        own = derive_evidence(recs).goals if recs else []
        if own:
            traits.goal = own[0]
        elif goals:
            traits.goal = goals[i % len(goals)]
        if traits.budget_usd is None:
            traits.budget_usd = {"tight": 40.0, "moderate": 120.0, "generous": 400.0}[traits.budget]


def replacement_traits(existing: list[Traits], seed: int) -> Traits:
    """A trait set far from every existing persona (used when regenerating a near-duplicate)."""
    rng = random.Random(seed)
    pool = _candidate_pool(rng, 1500)
    return pool[_farthest(pool, existing, set())]
