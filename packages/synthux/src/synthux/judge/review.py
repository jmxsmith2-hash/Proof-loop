"""The judge: every reported issue is checked before it can reach a report.

Rejects issues that are vague, unsupported by behaviour, or flattering, and
sends them back to the persona for specifics (up to `max_rounds`). The default
judge is rule-based and free; a stronger model can be configured for the
`judge` role.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from synthux.core.llm import LLMGateway, LLMRequest
from synthux.core.logging import get_logger
from synthux.elicitation.questions import PRAISE_WORDS, PROBLEM_SIGNALS
from synthux.runner.perception import perceive

log = get_logger(__name__)

PAGE_LEVEL = {"slow_response", "no_loading_feedback"}  # these need no on-screen region
NEGATIVE_WORDS = ("not", "no ", "hard", "slow", "confus", "miss", "can't", "cannot", "unclear", "error", "lost",
                  "nothing", "didn't", "never", "without", "faint", "small")


@dataclass
class Verdict:
    verdict: str                 # accept | revise | reject
    reasons: list[str] = field(default_factory=list)
    feedback: str = ""           # what the persona must add when sent back


def _signals_for(steps: list[dict[str, Any]], screen_id: str) -> list[dict[str, Any]]:
    return [s["signals_json"] for s in steps if s["screen_id"] == screen_id]


def behavioural_support(issue: dict[str, Any], steps: list[dict[str, Any]], slow_threshold: float) -> str | None:
    """Return the behaviour that supports this issue, or None if there is none."""
    cat = issue["category"]
    at = next((s for s in steps if s["idx"] == issue["step_idx"]), None)
    here = _signals_for(steps, issue["screen_id"])
    sig = at["signals_json"] if at else {}
    if cat == "unexplained_error":
        return "error shown after an action" if any(s.get("error_seen") for s in here) or sig.get("error_seen") else None
    if cat == "data_lost_on_error":
        return "entered data disappeared" if any(s.get("data_lost") for s in here) else None
    if cat == "ambiguous_label":
        return "hesitation while filling the field" if float(sig.get("confusion", 0)) >= 0.3 else None
    if cat == "unclear_input_format":
        return "the form was rejected" if any(s.get("error_seen") for s in here) else None
    if cat == "hidden_control":
        searched = sum(1 for s in here if s.get("scroll_search") or s.get("cannot_find"))
        return f"searched {searched} step(s) before finding a way forward" if searched >= 2 else None
    if cat in PAGE_LEVEL:
        seconds = float(sig.get("load_seconds", 0))
        if cat == "no_loading_feedback":
            return f"waited {seconds:.1f}s with a frozen screen" if sig.get("no_loading_feedback") else None
        return f"waited {seconds:.1f}s" if seconds > slow_threshold else None
    if cat == "dead_click":
        return "a click produced no visible change" if sig.get("no_change") else None
    # Elicited (stated) complaints: a measured problem must involve the same element - the persona
    # clicked on or next to it when the problem happened - and must not be its own simulated slip.
    region = issue.get("region_json")
    if not region:
        return None
    own_fault = {"no_change", "repeat_action"}
    for step in steps:
        if step["screen_id"] != issue["screen_id"]:
            continue
        signals = step["signals_json"]
        hits = [k for k in PROBLEM_SIGNALS if signals.get(k) and not (k in own_fault and signals.get("noise_applied"))]
        action = step.get("intended_json") or {}
        if hits and _near(action, region):
            return f"problems while using this element: {', '.join(hits)}"
    return None


def _near(action: dict[str, Any], region: dict[str, int], margin: int = 40) -> bool:
    x, y = action.get("x"), action.get("y")
    if x is None or y is None:
        return False
    return (region["x"] - margin <= x <= region["x"] + region["w"] + margin
            and region["y"] - margin <= y <= region["y"] + region["h"] + margin)


class RuleJudge:
    """Free, deterministic judge applying the rubric to the evidence."""

    def __init__(self, slow_threshold: float):
        self.slow_threshold = slow_threshold

    async def review(self, issue: dict[str, Any], steps: list[dict[str, Any]]) -> Verdict:
        if issue["failure_class"] != "UX_ISSUE":
            return Verdict("accept", ["agent limitation: kept out of UX findings, recorded separately"])
        lower = issue["description"].lower()  # the persona's words (titles are ours)
        reasons: list[str] = []
        if any(w in lower for w in PRAISE_WORDS) and not any(w in lower for w in NEGATIVE_WORDS):
            return Verdict("reject", ["flattering: praises instead of naming a problem"])
        specific = len(issue["description"].split()) >= 6 and (
            re.search(r"'[^']{2,}'", issue["description"]) or issue["category"] in PAGE_LEVEL
            or re.search(r"\d", issue["description"]))
        located = issue.get("region_json") or issue["category"] in PAGE_LEVEL or issue["category"] == "hidden_control"
        evidence = issue.get("evidence_json") or {}
        if not evidence.get("screenshot") or not evidence.get("steps") and issue["source"] == "behavior":
            return Verdict("reject", ["no evidence: missing screenshot or step sequence"])
        if not specific or not located:
            missing = []
            if not specific:
                missing.append("name the exact element or quote its text")
            if not located:
                missing.append("give its position on the screen")
            return Verdict("revise", ["vague: " + " and ".join(missing)], feedback="; ".join(missing))
        support = behavioural_support(issue, steps, self.slow_threshold)
        if support is None:
            return Verdict("reject", ["unsupported: no measured behaviour backs this up (stated opinion only)"])
        reasons.append(f"supported by behaviour: {support}")
        if evidence.get("noise_applied"):
            reasons.append("the step had a simulated misclick/typo; replay will check")
        return Verdict("accept", reasons)


JUDGE_SCHEMA = {
    "type": "object",
    "required": ["verdict", "reasons", "feedback"],
    "properties": {"verdict": {"type": "string", "enum": ["accept", "revise", "reject"]},
                   "reasons": {"type": "array", "items": {"type": "string"}},
                   "feedback": {"type": "string"}},
}


class ModelJudge:
    """Strong-model judge. Rules still run first, so the model never accepts unsupported issues."""

    def __init__(self, gateway: LLMGateway, slow_threshold: float):
        self.gateway = gateway
        self.rules = RuleJudge(slow_threshold)

    async def review(self, issue: dict[str, Any], steps: list[dict[str, Any]]) -> Verdict:
        first = await self.rules.review(issue, steps)
        if first.verdict != "accept" or issue["failure_class"] != "UX_ISSUE":
            return first
        shot = (issue.get("evidence_json") or {}).get("screenshot")
        images = []
        if shot:
            buf = io.BytesIO()
            Image.open(shot).convert("RGB").save(buf, "PNG")
            images.append(buf.getvalue())
        prompt = (
            "You are a strict usability research lead reviewing one reported issue from a synthetic tester.\n"
            f"Issue: {issue['title']} - {issue['description']}\nCategory: {issue['category']}  "
            f"Severity claimed: {issue['severity']}  Region: {issue.get('region_json')}\n"
            f"Measured support: {first.reasons}\nThink-aloud at that moment: "
            f"{(issue.get('evidence_json') or {}).get('think_aloud')}\n"
            "Accept only if the screenshot shows the problem and a real person would plausibly struggle too. "
            "Reject if it is vague, not visible, flattering, or only about the tester's own mistake. "
            "Use 'revise' if it is real but needs a more specific description; say exactly what to add."
        )
        data = (await self.gateway.complete(LLMRequest(role="judge", system="You review usability findings.",
                                                       prompt=prompt, schema=JUDGE_SCHEMA, images=images,
                                                       max_tokens=800))).data
        return Verdict(data["verdict"], first.reasons + list(data["reasons"]), data["feedback"])


def revise_issue(issue: dict[str, Any], feedback: str) -> dict[str, Any] | None:
    """Heuristic persona answering the judge: add the exact on-screen text and position from its screenshot."""
    evidence = issue.get("evidence_json") or {}
    shot = evidence.get("screenshot")
    region = issue.get("region_json")
    if not shot:
        return None
    buf = io.BytesIO()
    Image.open(shot).convert("RGB").save(buf, "PNG")
    screen = perceive(buf.getvalue())
    if region:
        inside = [t for t in screen.texts if t.x0 >= region["x"] - 10 and t.y0 >= region["y"] - 10
                  and t.x1 <= region["x"] + region["w"] + 10 and t.y1 <= region["y"] + region["h"] + 10]
    else:  # no location given: look for the quoted text on the persona's own screenshot
        quoted = re.findall(r"'([^']{2,})'", issue["description"])
        inside = [t for q in quoted for t in screen.find(q, 0.8)]
    if not inside:
        return None
    element = inside[0]
    horizontal = "left" if element.center[0] < screen.width / 3 else "right" if element.center[0] > 2 * screen.width / 3 else "centre"
    vertical = "top" if element.center[1] < screen.height / 3 else "bottom" if element.center[1] > 2 * screen.height / 3 else "middle"
    revised = dict(issue)
    revised["description"] = (f"{issue['description'].rstrip('.')}: the element '{element.text}' at the "
                              f"{vertical} {horizontal} of the screen.")
    revised["region_json"] = element.region(8)
    return revised
