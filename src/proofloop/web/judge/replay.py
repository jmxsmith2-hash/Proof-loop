"""Automatic replay: re-execute the persona's *intended* steps (no misclicks or typos)
in a fresh browser and check that each issue happens again.

Statuses: CONFIRMED, NOT_REPRODUCED, REPLAY_FAILED (could not get back to the
same screen for reasons not explained by noise), SKIPPED (agent limitations).
"""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

from PIL import Image

from proofloop.core.config import Settings
from proofloop.core.logging import get_logger
from proofloop.personas.models import Persona
from proofloop.web.calibration.profiles import CalibrationProfile
from proofloop.web.runner.behavior import PatienceBudget
from proofloop.web.runner.browser import BrowserPool
from proofloop.web.runner.observe import DEEP_NAV_PAGES, compute_signals, screen_id, visible_errors
from proofloop.web.runner.perception import Screen, fuzzy_ratio, hash_distance, perceive, perceive_async
from proofloop.web.runner.types import Action, Decision, Signals, ThinkAloud
from proofloop.web.study import Study

log = get_logger(__name__)

SIGNAL_CHECKS = {
    "slow_response": lambda s: s.slow_load,
    "no_loading_feedback": lambda s: s.slow_load and s.no_loading_feedback,
    "dead_click": lambda s: s.no_change and not s.loading,
    "unexplained_error": lambda s: s.error_seen and s.unexplained_error,
}
SLOW_CATEGORIES = {"slow_response", "no_loading_feedback"}
REGION_CATEGORIES = {"ambiguous_label", "hidden_control", "unclear_input_format", "elicited", "data_lost_on_error"}


def _png(path: str) -> bytes:
    buf = io.BytesIO()
    Image.open(path).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _region_text(screen: Screen, region: dict[str, int] | None) -> str:
    if not region:
        return ""
    texts = [t.text for t in screen.texts if t.x0 >= region["x"] - 12 and t.y0 >= region["y"] - 12
             and t.x1 <= region["x"] + region["w"] + 12 and t.y1 <= region["y"] + region["h"] + 12]
    return " ".join(texts)


def load_trigger_step(steps: list[dict[str, Any]], issue_idx: int) -> int:
    """The step whose click started the slow load (walk back over waits and impatient re-clicks)."""
    by_idx = {s["idx"]: s for s in steps}
    idx = issue_idx
    while idx - 1 in by_idx:
        prev = by_idx[idx - 1]["signals_json"]
        if prev.get("no_change") and prev.get("loading"):
            idx -= 1
        else:
            break
    return idx


def sequence_key(session: dict[str, Any], steps: list[dict[str, Any]], upto: int) -> str:
    actions = [s["intended_json"] for s in steps if s["idx"] <= upto]
    cond = session["conditions_json"]
    raw = json.dumps([session["task_id"], cond.get("device"), cond.get("network"), actions], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()


class Replayer:
    def __init__(self, settings: Settings, profile: CalibrationProfile, study: Study, *, sandbox: bool):
        self.settings = settings
        self.profile = profile
        self.study = study
        self.sandbox = sandbox
        self.vault = study.vault()
        self.cache: dict[str, dict[str, Any]] = {}

    async def replay(self, pool: BrowserPool, persona: Persona, session: dict[str, Any],
                     steps: list[dict[str, Any]], issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Replay one session up to its last issue step. Returns {issue_id: {status, detail}}."""
        results: dict[str, dict[str, Any]] = {}
        todo = []
        for issue in issues:
            if issue["failure_class"] != "UX_ISSUE":
                results[issue["id"]] = {"status": "SKIPPED", "detail": "agent limitation; not a UX finding"}
            else:
                todo.append(issue)
        slow = [i for i in todo if i["category"] in SLOW_CATEGORIES]
        rest = [i for i in todo if i["category"] not in SLOW_CATEGORIES]
        if rest:
            upto = max(i["step_idx"] for i in rest)
            trace = await self._cached(pool, persona, session, steps, upto, long_wait_at=None)
            for issue in rest:
                results[issue["id"]] = self._judge(issue, steps, trace)
        for issue in slow:
            # Click the control that started the slow load once, then time the load fully.
            trigger = load_trigger_step(steps, issue["step_idx"])
            trace = await self._cached(pool, persona, session, steps, trigger, long_wait_at=trigger)
            results[issue["id"]] = self._judge_slow(issue, steps, trace, trigger)
        return results

    async def _cached(self, pool: BrowserPool, persona: Persona, session: dict[str, Any],
                      steps: list[dict[str, Any]], upto: int, long_wait_at: int | None) -> dict[str, Any]:
        key = sequence_key(session, steps, upto) + f"|{long_wait_at}"
        if key not in self.cache:
            self.cache[key] = await self._run(pool, persona, session, steps, upto, long_wait_at)
        return self.cache[key]

    def _judge_slow(self, issue: dict[str, Any], steps: list[dict[str, Any]], trace: dict[str, Any],
                    trigger: int) -> dict[str, Any]:
        signals: Signals | None = trace["signals"].get(trigger)
        if signals is None:
            return {"status": "REPLAY_FAILED", "detail": trace["failed"] or "could not replay the trigger step"}
        ok = SIGNAL_CHECKS[issue["category"]](signals)
        return {"status": "CONFIRMED" if ok else "NOT_REPRODUCED",
                "detail": f"replayed load took {signals.load_seconds:.1f}s; signals {signals.active()}"}

    async def _run(self, pool: BrowserPool, persona: Persona, session: dict[str, Any],
                   steps: list[dict[str, Any]], upto: int, long_wait_at: int | None = None) -> dict[str, Any]:
        cond = session["conditions_json"]
        task = self.study.task(session["task_id"])
        budget = PatienceBudget.for_persona(persona.traits, self.profile, time_pressure=bool(cond.get("time_pressure")))
        browser = await pool.session(device=self.settings.device(cond["device"]),
                                     network=self.settings.network(cond.get("network", "fast")),
                                     safety=self.settings.safety, sandbox=self.sandbox,
                                     brand_mask=self.study.target.brand_mask)
        trace: dict[str, Any] = {"screens": {}, "signals": {}, "failed": None, "path": []}
        try:
            await browser.goto(self.study.task_url(task))
            trace["path"].append(screen_id(browser.url))
            png = await browser.screenshot()
            png, _ = await browser.wait_for_settle(png, max_wait=1.0)
            screen = await perceive_async(png)
            pending = 0.0
            history: list[Any] = []
            for step in steps:
                if step["idx"] > upto:
                    break
                trace["screens"][step["idx"]] = screen
                action = Action.from_dict(step["intended_json"])
                if action.kind == "quit":
                    break
                if step["idx"] == cond.get("interruption_step") and cond.get("interruption_reload"):
                    await browser.page.reload(wait_until="load")
                await browser.execute(action, self.vault)
                wait = 1.0 if action.kind == "scroll" else budget.wait_tolerance_s
                if step["idx"] == long_wait_at:
                    wait = 25.0
                new_png, timing = await browser.wait_for_settle(screen.png, max_wait=wait)
                new_screen = await perceive_async(new_png)
                decision = Decision(action=action, think=ThinkAloud("", "", "", ""))
                signals = compute_signals(prev=screen, cur=new_screen, decision=decision, executed=action,
                                          timing=timing, pending_wait_s=pending,
                                          slow_threshold_s=self.settings.browser.slow_load_seconds,
                                          history=[], noise_applied=False, loading=browser.nav_pending)
                pending = pending + timing.settled_s if (not timing.responded and browser.nav_pending) else 0.0
                trace["signals"][step["idx"]] = signals
                trace["screens"][f"after_{step['idx']}"] = new_screen
                if trace["path"][-1] != screen_id(browser.url):
                    trace["path"].append(screen_id(browser.url))
                history.append(signals)
                screen = new_screen
        except Exception as exc:  # replay itself broke (browser error, safety refusal)
            log.warning("replay of %s failed: %s", session["id"], exc)
            trace["failed"] = str(exc)
        finally:
            await browser.close()
        return trace

    def _judge(self, issue: dict[str, Any], steps: list[dict[str, Any]], trace: dict[str, Any]) -> dict[str, Any]:
        idx = issue["step_idx"]
        step = next((s for s in steps if s["idx"] == idx), None)
        if step is None:
            return {"status": "REPLAY_FAILED", "detail": "issue step not recorded"}
        replayed: Screen | None = trace["screens"].get(idx)
        noisy_before = any(s["signals_json"].get("noise_applied") for s in steps if s["idx"] <= idx)
        if replayed is None:
            reason = trace["failed"] or "replay stopped before the issue step"
            status = "NOT_REPRODUCED" if noisy_before else "REPLAY_FAILED"
            return {"status": status, "detail": reason}
        distance = hash_distance(replayed.dhash, int(step["screen_hash"]))
        same_screen = distance <= self.profile.judge.replay_hash_threshold
        if not same_screen:
            status = "NOT_REPRODUCED" if noisy_before else "REPLAY_FAILED"
            return {"status": status, "hash_distance": distance,
                    "detail": "replay reached a different screen" + (" (original path had a misclick/typo)" if noisy_before else "")}
        category = issue["category"]
        if category == "deep_navigation":
            pages = list(dict.fromkeys(trace["path"]))
            ok = len(pages) >= DEEP_NAV_PAGES
            return {"status": "CONFIRMED" if ok else "NOT_REPRODUCED", "hash_distance": distance,
                    "detail": f"replay went through {len(pages)} pages: {' -> '.join(trace['path'])}"}
        if category in SIGNAL_CHECKS:
            signals: Signals | None = trace["signals"].get(idx)
            ok = signals is not None and SIGNAL_CHECKS[category](signals)
            return {"status": "CONFIRMED" if ok else "NOT_REPRODUCED", "hash_distance": distance,
                    "detail": f"replayed signals: {signals.active() if signals else 'none'}"}
        if category == "unclear_input_format":
            earlier = [trace["screens"].get(f"after_{i}") for i in range(idx)]
            ok = any(sc is not None and visible_errors(sc) for sc in earlier)
            return {"status": "CONFIRMED" if ok else "NOT_REPRODUCED", "hash_distance": distance,
                    "detail": "the form rejected the earlier entry on replay" if ok else "no rejection on replay"}
        if category == "data_lost_on_error":
            after = trace["screens"].get(f"after_{idx - 1}") or replayed
            ok = bool(visible_errors(after) or visible_errors(replayed)) and all(b.empty for b in replayed.inputs)
            return {"status": "CONFIRMED" if ok else "NOT_REPRODUCED", "hash_distance": distance,
                    "detail": "fields empty after the error" if ok else "fields kept their data"}
        original = perceive(_png(step["screenshot_path"]))
        before_text = _region_text(original, issue.get("region_json"))
        after_text = _region_text(replayed, issue.get("region_json"))
        if category == "hidden_control" and not before_text:
            ok = same_screen
        else:
            ok = bool(before_text) and fuzzy_ratio(before_text, after_text) >= 0.75
        return {"status": "CONFIRMED" if ok else "NOT_REPRODUCED", "hash_distance": distance,
                "detail": f"element '{before_text[:40]}' {'present' if ok else 'not found'} on replay"}
