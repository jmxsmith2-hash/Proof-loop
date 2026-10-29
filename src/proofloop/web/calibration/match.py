"""Match real-user issues to synthetic findings for the same tasks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from proofloop.core.similarity import similarity_matrix

CATEGORY_HINTS = {
    "unexplained_error": ["error", "0x", "code", "message", "went wrong", "failed"],
    "slow_response": ["slow", "load", "wait", "took long", "forever", "loading", "spinner"],
    "no_loading_feedback": ["spinner", "no feedback", "froze", "frozen", "nothing happened"],
    "ambiguous_label": ["label", "what id", "which id", "meaning", "unclear field", "jargon", "id field", "dob"],
    "unclear_input_format": ["format", "date", "dd/mm", "mm/dd", "phone number format"],
    "data_lost_on_error": ["cleared", "lost", "erased", "wiped", "deleted my", "start over", "re-enter", "retype"],
    "hidden_control": ["find", "couldn't see", "could not see", "hidden", "contrast", "button", "grey", "gray", "missed"],
    "dead_click": ["did nothing", "doesn't respond", "unresponsive", "not clickable"],
}
MATCH_THRESHOLD = 0.45


def infer_category(text: str) -> str | None:
    lower = text.lower()
    scores = {cat: sum(1 for h in hints if h in lower) for cat, hints in CATEGORY_HINTS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else None


@dataclass
class Match:
    real: dict[str, Any]
    synthetic: dict[str, Any]
    score: float


def _syn_text(c: dict[str, Any]) -> str:
    return f"{c['title']} {c['category'].replace('_', ' ')} {c['detail_json'].get('description', '')}"


def _real_text(r: dict[str, Any]) -> str:
    return f"{r['title']} {r.get('description') or ''}"


def match_issues(real: list[dict[str, Any]], synthetic: list[dict[str, Any]]) -> tuple[list[Match], list, list]:
    """Greedy one-to-one matching. Returns (both found, real only, synthetic only)."""
    if not real or not synthetic:
        return [], list(real), list(synthetic)
    texts = [_real_text(r) for r in real] + [_syn_text(s) for s in synthetic]
    sim = similarity_matrix(texts)
    candidates = []
    for i, r in enumerate(real):
        r_cat = r.get("category") or infer_category(_real_text(r))
        for j, s in enumerate(synthetic):
            same_task = not r.get("task_id") or r["task_id"] in s["detail_json"].get("tasks", [])
            if not same_task:
                continue
            same_screen = bool(r.get("screen")) and r["screen"] == s["screen_id"]
            same_cat = r_cat is not None and (r_cat == s["category"] or
                                              {r_cat, s["category"]} == {"slow_response", "no_loading_feedback"})
            score = 0.45 * float(sim[i, len(real) + j]) + 0.3 * same_screen + 0.35 * same_cat
            if score >= MATCH_THRESHOLD:
                candidates.append((score, i, j))
    used_r, used_s, matches = set(), set(), []
    for score, i, j in sorted(candidates, reverse=True):
        if i in used_r or j in used_s:
            continue
        used_r.add(i)
        used_s.add(j)
        matches.append(Match(real[i], synthetic[j], round(score, 3)))
    real_only = [r for i, r in enumerate(real) if i not in used_r]
    synthetic_only = [s for j, s in enumerate(synthetic) if j not in used_s]
    return matches, real_only, synthetic_only
