"""Diversity checks: personas (and their feedback) must not be near-copies of each other."""

from __future__ import annotations

from dataclasses import dataclass

from synthux.core.similarity import similarity_matrix
from synthux.personas.design import trait_similarity
from synthux.personas.models import Persona

DESCRIPTION_THRESHOLD = 0.80   # combined text + trait similarity above this = too alike
FEEDBACK_THRESHOLD = 0.85      # think-aloud / complaints text similarity above this = too alike


@dataclass
class SimilarPair:
    a: str
    b: str
    score: float
    text_score: float
    trait_score: float


def description_pairs(personas: list[Persona], threshold: float = DESCRIPTION_THRESHOLD) -> list[SimilarPair]:
    """Pairs of personas whose narrative and traits are both too similar."""
    if len(personas) < 2:
        return []
    texts = similarity_matrix([p.narrative for p in personas])
    pairs = []
    for i in range(len(personas)):
        for j in range(i + 1, len(personas)):
            t_score = trait_similarity(personas[i].traits, personas[j].traits)
            combined = 0.5 * float(texts[i, j]) + 0.5 * t_score
            if combined >= threshold:
                pairs.append(SimilarPair(personas[i].id, personas[j].id, round(combined, 3),
                                         round(float(texts[i, j]), 3), round(t_score, 3)))
    return sorted(pairs, key=lambda p: -p.score)


def feedback_pairs(feedback: dict[str, str], threshold: float = FEEDBACK_THRESHOLD) -> list[SimilarPair]:
    """Pairs of personas whose collected feedback text is too similar (checked after a run)."""
    ids = [k for k, v in feedback.items() if v.strip()]
    if len(ids) < 2:
        return []
    sims = similarity_matrix([feedback[i] for i in ids])
    pairs = []
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            score = float(sims[x, y])
            if score >= threshold:
                pairs.append(SimilarPair(ids[x], ids[y], round(score, 3), round(score, 3), 0.0))
    return sorted(pairs, key=lambda p: -p.score)
