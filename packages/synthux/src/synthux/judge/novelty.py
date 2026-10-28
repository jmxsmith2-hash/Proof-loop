"""Separate "unfamiliar" from "actually hard".

An issue is labelled NOVELTY (and ranked lower) when it mostly affects personas
who are new to this kind of product, they get past it (in the session or on a
return visit), and it does not break an established convention. Convention
violations (unexplained errors, lost data, unexplained formats, slow pages
without feedback, controls below WCAG contrast) are never treated as novelty.
"""

from __future__ import annotations

from typing import Any

from synthux.calibration.profiles import NoveltyParams
from synthux.personas.models import Persona

CONVENTION_VIOLATIONS = {
    "unexplained_error": "error messages should say what went wrong and how to fix it",
    "data_lost_on_error": "forms should keep what the user typed after an error",
    "unclear_input_format": "required input formats should be shown before submitting",
    "slow_response": "pages should respond within a few seconds",
    "no_loading_feedback": "long waits should show progress",
}


def is_convention_violation(category: str, issues: list[dict[str, Any]]) -> str | None:
    if category in CONVENTION_VIOLATIONS:
        return CONVENTION_VIOLATIONS[category]
    if category == "hidden_control":
        text = " ".join(i["description"] for i in issues)
        if ":1 contrast" in text or "found no way to continue" in text:
            return "controls need enough contrast (WCAG 1.4.11) to be found"
    return None


def classify(category: str, issues: list[dict[str, Any]], personas: dict[str, Persona],
             sessions: dict[str, dict[str, Any]], params: NoveltyParams) -> tuple[bool, str]:
    violation = is_convention_violation(category, issues)
    if violation:
        return False, f"breaks a convention: {violation}"
    affected = {i["persona_id"] for i in issues}
    if not affected:
        return False, "no affected personas"
    low = {p for p in affected if p in personas and personas[p].traits.familiarity <= params.max_familiarity}
    share = len(low) / len(affected)
    affected_sessions = [sessions[i["session_id"]] for i in issues if i["session_id"] in sessions]
    recovered = sum(1 for s in affected_sessions if s["outcome"] == "completed") / max(1, len(affected_sessions))
    later_visits = [s for s in sessions.values() if s["persona_id"] in affected and s["visit_index"] > 0
                    and s["task_id"] == issues[0]["task_id"]]
    gone_later = bool(later_visits) and not any(sessions[i["session_id"]]["visit_index"] > 0 for i in issues)
    if share >= params.min_share_low_familiarity and (recovered >= 0.5 or gone_later):
        how = "disappeared on return visits" if gone_later else f"{recovered:.0%} still finished"
        return True, f"{share:.0%} of affected personas are new to this kind of product and {how}"
    return False, f"affects experienced personas too ({share:.0%} low familiarity)"
