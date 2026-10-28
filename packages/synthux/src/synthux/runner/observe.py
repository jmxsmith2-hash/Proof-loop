"""Measured signals and issue candidates, computed by code from pixels and timings.

Frustration and behavioural findings come from here, not from what a persona says.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from synthux.runner.perception import Screen, TextBox
from synthux.runner.types import Action, Decision, IssueCandidate, Observation, Signals, StepRecord, Timing

ERROR_WORDS = ["error", "invalid", "failed", "went wrong", "incorrect", "not valid", "problem", "unable",
               "denied", "oops", "could not", "couldn't"]
EXPLANATION_WORDS = ["because", "must", "should", "please", "check", "try again", "enter a", "use the",
                     "format", "required", "at least", "doesn't match", "does not match", "too short",
                     "missing", "contact", "typo", "is incorrect", "was declined"]
CAPTCHA_WORDS = ["captcha", "i'm not a robot", "im not a robot", "verify you are human", "select all images"]
BROWSER_ERROR_WORDS = ["this site can't be reached", "this site cant be reached", "err_", "aw, snap",
                       "took too long to respond", "refused to connect", "this page isn't working"]
UNSUPPORTED_WORDS = ["drag and drop", "drag here", "choose file", "upload a file", "slide to verify"]

SEVERITY = {1: "cosmetic", 2: "minor", 3: "major", 4: "blocker"}


def visible_errors(screen: Screen) -> list[TextBox]:
    return [t for t in screen.texts if any(w in t.norm for w in ERROR_WORDS)]


def explained(error: TextBox, screen: Screen) -> bool:
    """An error is explained when it, or text right next to it, gives a cause or a next step."""
    nearby = [t for t in screen.texts if abs(t.center[1] - error.center[1]) <= 70]
    blob = " ".join(t.norm for t in nearby)
    return any(w in blob for w in EXPLANATION_WORDS)


def screen_id(url: str) -> str:
    """Stable id for grouping (path of the page). Used by reports only, never shown to a persona."""
    parts = urlsplit(url)
    return parts.path or "/"


# ------------------------------------------------------------ limitations


def limitation_on_screen(screen: Screen) -> str | None:
    text = screen.norm_text
    if any(w in text for w in CAPTCHA_WORDS):
        return "captcha"
    if any(w in text for w in BROWSER_ERROR_WORDS):
        return "technical_error"
    return None


def unsupported_interaction(screen: Screen) -> bool:
    return any(w in screen.norm_text for w in UNSUPPORTED_WORDS)


# ---------------------------------------------------------------- signals


def compute_signals(
    *,
    prev: Screen,
    cur: Screen,
    decision: Decision,
    executed: Action | None,
    timing: Timing | None,
    pending_wait_s: float,
    slow_threshold_s: float,
    history: list[StepRecord],
    noise_applied: bool,
    loading: bool = False,
    load_trigger: str = "",
) -> Signals:
    action = decision.action
    s = Signals(confusion=round(decision.confusion, 2), noise_applied=noise_applied, loading=loading,
                load_trigger=load_trigger or action.target)
    changed = timing is not None and timing.responded
    interactive = action.kind in ("click", "fill", "press", "back")
    if interactive and not changed:
        s.no_change = True
    if action.kind == "scroll" and not changed:
        s.scroll_end = True
    if action.kind == "wait" and pending_wait_s > 0 and not changed:
        s.no_change = True
    if changed and (interactive or action.kind == "wait") and timing is not None:
        reacted_at = timing.major_change_s if timing.major_change_s is not None else timing.first_change_s
        s.load_seconds = round(pending_wait_s + (reacted_at or 0.0), 2)
        if s.load_seconds > slow_threshold_s:
            s.slow_load = True
            s.no_loading_feedback = timing.intermediate_states == 0
    errors_now = visible_errors(cur)
    errors_before = {e.norm for e in visible_errors(prev)}
    submitted = action.kind in ("click", "press") and changed
    new_error = bool(errors_now) and ({e.norm for e in errors_now} - errors_before or submitted)
    if new_error and action.kind not in ("scroll", "wait", "fill"):
        s.error_seen = True
        s.error_text = errors_now[0].text
        s.error_region = errors_now[0].region(6)
        s.unexplained_error = not explained(errors_now[0], cur)
    if history and action.kind in ("click", "fill"):
        prev_step = history[-1]
        same_target = prev_step.intended.target == action.target and prev_step.intended.kind == action.kind
        if same_target and (prev_step.signals.no_change or prev_step.signals.error_seen):
            s.repeat_action = True
    s.backtrack = action.kind == "back"
    kinds = {o.kind for o in decision.observations}
    s.data_lost = "data_lost_on_error" in kinds
    s.cannot_find = "cannot_find" in kinds
    s.scroll_search = action.kind == "scroll" and "remembered" not in kinds
    if changed and interactive and not errors_now:
        s.progress = True
    return s


# ------------------------------------------------------------------ issues

TITLES = {
    "ambiguous_label": "Field label is ambiguous",
    "unclear_input_format": "Required input format is not explained",
    "data_lost_on_error": "Form clears entered data after an error",
    "unexplained_error": "Error message gives no cause or next step",
    "hidden_control": "Control needed to continue is hard to find",
    "slow_response": "Slow response after an action",
    "no_loading_feedback": "No loading feedback during a long wait",
    "dead_click": "Click on a control had no visible effect",
}


def issues_from_step(step: StepRecord, decision: Decision, *, slow_threshold_s: float,
                     history: list[StepRecord]) -> list[IssueCandidate]:
    """Turn what happened in one step into issue candidates (verified later by the judge)."""
    found: dict[str, IssueCandidate] = {}
    trail = [h.intended.describe() for h in history] + [step.intended.describe()]

    def add(category: str, description: str, severity: int, region: dict | None, extra: dict | None = None,
            screen: str | None = None) -> None:
        if category in found and found[category].severity >= severity:
            return
        found[category] = IssueCandidate(
            category=category, title=TITLES.get(category, category.replace("_", " ").capitalize()),
            description=description, severity=severity, step_idx=step.idx, screen_id=screen or step.screen_id,
            region=region, evidence={"screenshot": step.screenshot_path, "steps": trail,
                                     "think_aloud": step.think.__dict__, "signals": step.signals.active(),
                                     "noise_applied": step.signals.noise_applied, **(extra or {})})

    for obs in decision.observations:
        if obs.kind in ("interruption", "remembered", "missed_field"):
            continue
        if obs.kind == "cannot_find":
            if obs.severity >= 4:
                add("hidden_control", "Searched the whole page (down, a careful look, back up) and found no way "
                                      "to continue", 4, obs.region)
            continue
        add(obs.kind, obs.detail, obs.severity, obs.region)
    sig = step.signals
    if sig.unexplained_error and "unexplained_error" not in found:
        add("unexplained_error", f"Error '{sig.error_text}' appears without a cause or a next step", 3,
            sig.error_region)
    if sig.slow_load:
        sev = 3 if sig.load_seconds > 2 * slow_threshold_s else 2
        trigger = sig.load_trigger or step.intended.target or step.intended.kind
        after = step.screen_id_after or step.screen_id
        add("slow_response", f"The page took {sig.load_seconds:.1f}s to appear after clicking '{trigger}'", sev, None,
            {"load_seconds": sig.load_seconds, "from_screen": step.screen_id}, screen=after)
        if sig.no_loading_feedback:
            add("no_loading_feedback", f"Nothing on the page showed progress during the {sig.load_seconds:.1f}s wait "
                                       f"after clicking '{trigger}'", 2, None,
                {"load_seconds": sig.load_seconds, "from_screen": step.screen_id}, screen=after)
    if (sig.no_change and step.intended.kind == "click" and step.intended.target
            and not sig.slow_load and not sig.loading):
        add("dead_click", f"Clicking '{step.intended.target}' did nothing visible", 2,
            {"x": (step.executed.x or 0) - 20, "y": (step.executed.y or 0) - 15, "w": 40, "h": 30}
            if step.executed and step.executed.x is not None else None)
    return list(found.values())


def observation_from_limitation(kind: str) -> Observation:
    detail = {"captcha": "a CAPTCHA blocks the automated persona",
              "technical_error": "the browser showed a technical error page",
              "unsupported_interaction": "the task needs an interaction the agent cannot perform",
              "blocked_by_safety": "the safety sandbox blocked this action"}.get(kind, kind)
    return Observation(kind, detail)
