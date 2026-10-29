"""One persona attempting one task: the screenshot -> decide -> act loop."""

from __future__ import annotations

import io
import itertools
import random
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image

from proofloop.core.config import Settings
from proofloop.core.db import new_id
from proofloop.core.llm import LLMError
from proofloop.core.logging import get_logger
from proofloop.personas.models import Persona
from proofloop.web.calibration.profiles import CalibrationProfile
from proofloop.web.engines.base import Actor, ActorContext
from proofloop.web.engines.heuristic import modal_visible
from proofloop.web.runner.behavior import Frustration, Noise, PatienceBudget
from proofloop.web.runner.browser import BrowserPool, BrowserSession
from proofloop.web.runner.memory import SessionMemory
from proofloop.web.runner.observe import (
    DEEP_NAV_PAGES,
    compute_signals,
    issues_from_step,
    limitation_on_screen,
    screen_id,
    unsupported_interaction,
)
from proofloop.web.runner.perception import Screen, fuzzy_contains, hash_distance, normalize, perceive_async
from proofloop.web.runner.types import (
    Action,
    Decision,
    IssueCandidate,
    SessionResult,
    Signals,
    StepRecord,
    ThinkAloud,
    Timing,
)
from proofloop.web.safety.allowlist import SafetyError
from proofloop.web.safety.guard import ActionGuard
from proofloop.web.study import Conditions, Study, TaskSpec

log = get_logger(__name__)

# How much of the patience budget one action uses.
STEP_COST = {"click": 1.0, "back": 1.0, "fill": 0.35, "type": 0.35, "press": 0.5, "scroll": 0.5, "wait": 0.5}

CAUSE_WORDS = {
    "unexplained_error": "the errors don't explain anything", "data_lost": "it keeps deleting what I typed",
    "no_change": "clicking does nothing", "slow_load": "it is far too slow",
    "cannot_find": "I can't find how to continue", "confusion": "I don't understand what it wants",
    "error_seen": "it keeps showing errors", "repeat_action": "I keep repeating the same thing",
    "scroll_search": "I've been searching for ages", "no_loading_feedback": "nothing shows it is working",
    "backtrack": "I keep going back and forth",
}


def is_success(task: TaskSpec, screen: Screen, url: str) -> bool:
    if task.success_url and task.success_url in url:
        return True
    text = screen.norm_text
    return any(fuzzy_contains(text, normalize(phrase), 0.85) for phrase in task.success_text)


def _save_jpeg(png: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.open(io.BytesIO(png)).convert("RGB").save(path, "JPEG", quality=72)


class SessionRunner:
    def __init__(self, settings: Settings, profile: CalibrationProfile, study: Study, *, sandbox: bool,
                 out_dir: Path):
        self.settings = settings
        self.profile = profile
        self.study = study
        self.sandbox = sandbox
        self.out_dir = out_dir
        self.guard = ActionGuard(settings.safety, sandbox=sandbox)
        self.vault = study.vault()
        self._a11y_seen: list[tuple[str, str, int]] = []

    def conditions_for(self, persona: Persona, conditions: Conditions) -> dict[str, Any]:
        t = persona.traits
        return {
            "device": conditions.device or t.device,
            "network": conditions.network,
            "time_pressure": t.time_pressure if conditions.time_pressure is None else conditions.time_pressure,
            "interruption_step": conditions.interruption_step,
            "interruption_memory_keep": conditions.interruption_memory_keep,
            "interruption_reload": conditions.interruption_reload,
        }

    async def run(self, pool: BrowserPool, persona: Persona, task: TaskSpec, actor: Actor, *, seed: int,
                  repeat_index: int = 0, visit_index: int = 0, memory: SessionMemory | None = None,
                  conditions: Conditions | None = None, session_id: str | None = None) -> SessionResult:
        session_id = session_id or new_id("ses")
        cond = self.conditions_for(persona, conditions or self.study.conditions)
        traits = persona.traits
        budget = PatienceBudget.for_persona(traits, self.profile, time_pressure=cond["time_pressure"],
                                            task_cap=task.max_steps)
        slow = self.settings.browser.slow_load_seconds
        frustration = Frustration.for_persona(traits, self.profile, time_pressure=cond["time_pressure"],
                                              slow_threshold_s=slow)
        rng = random.Random(seed)
        noise = Noise(self.settings.noise, traits, random.Random(seed ^ 0x5EED))
        memory = memory or SessionMemory()
        device = self.settings.device(cond["device"])
        browser = await pool.session(device=device, network=self.settings.network(cond["network"]),
                                     safety=self.settings.safety, sandbox=self.sandbox,
                                     brand_mask=self.study.target.brand_mask)
        state = _LoopState(session_id=session_id, started=time.monotonic())
        try:
            await self._loop(browser, persona, task, actor, budget, frustration, noise, rng, memory, cond, seed, state)
        finally:
            await browser.close()
        if state.outcome == "completed":
            memory.commit_target()
        if task.type == "find":
            state.issues.extend(navigation_issues(state.steps, state.outcome))
        result = SessionResult(
            session_id=session_id, persona_id=persona.id, task_id=task.id, repeat_index=repeat_index,
            visit_index=visit_index, seed=seed, outcome=state.outcome, quit_reason=state.quit_reason,
            steps=state.steps, issues=_dedupe(state.issues), sim_seconds=round(state.sim_seconds, 1),
            wall_seconds=round(time.monotonic() - state.started, 1), final_frustration=round(frustration.score, 1),
            frustration_timeline=frustration.timeline, memory=memory.long_term(), conditions=cond,
            a11y=state.a11y)
        log.info("%s %s/%s rep%d visit%d: %s after %d steps (frustration %.0f/%.0f)", session_id, persona.label,
                 task.id, repeat_index, visit_index, result.outcome, len(result.steps), frustration.score,
                 frustration.threshold)
        return result

    # ------------------------------------------------------------------ loop
    async def _loop(self, browser: BrowserSession, persona: Persona, task: TaskSpec, actor: Actor,
                    budget: PatienceBudget, frustration: Frustration, noise: Noise, rng: random.Random,
                    memory: SessionMemory, cond: dict[str, Any], seed: int, state: _LoopState) -> None:
        try:
            await browser.goto(self.study.task_url(task))
        except SafetyError:
            raise
        except Exception as exc:
            state.fail("agent_failure", f"could not open the start page: {exc}", "technical_error")
            return
        png = await browser.screenshot()
        png, _ = await browser.wait_for_settle(png, max_wait=1.0)
        screen = await perceive_async(png)
        last_signals: Signals | None = None
        last_timing: Timing | None = None
        pending_wait = 0.0
        pending_trigger = ""
        interrupted = False
        skill_pace = {1: 1.5, 2: 1.25, 3: 1.0, 4: 0.85, 5: 0.75}[persona.traits.tech_skill]

        for idx in itertools.count():
            # Patience is spent on decisions, not keystrokes: typing into a field costs a fraction of a step.
            if state.effort >= budget.max_steps or idx >= budget.max_steps * 3:
                break
            url = browser.url
            shot_path = self.out_dir / state.session_id / f"{idx:03d}.jpg"
            _save_jpeg(screen.png, shot_path)
            if is_success(task, screen, url):
                state.outcome = "completed"
                return
            limitation = limitation_on_screen(screen)
            if limitation:
                state.fail("agent_failure", f"stopped by {limitation.replace('_', ' ')}", limitation,
                           step_idx=idx, screen=screen_id(url), shot=str(shot_path))
                return
            if cond["interruption_step"] is not None and idx == cond["interruption_step"]:
                forgotten = memory.degrade(cond["interruption_memory_keep"], rng)
                state.sim_interrupt_s += 300
                interrupted = True
                if cond["interruption_reload"]:
                    # The persona knows the page was reset while they were away; that is not the product's fault.
                    memory.typed.clear()
                    memory.typed_kinds.clear()
                    await browser.page.reload(wait_until="load")
                    png = await browser.screenshot()
                    screen = await perceive_async(png)
                    _save_jpeg(screen.png, shot_path)
                    url = browser.url
                log.debug("interrupted at step %d; forgot %d facts", idx, len(forgotten))
            ctx = ActorContext(persona=persona, task=task, screen=screen, memory=memory, budget=budget, step=idx,
                               frustration=frustration.score, frustration_threshold=frustration.threshold,
                               last_signals=last_signals, last_timing=last_timing, browser_busy=browser.nav_pending,
                               interrupted=interrupted, framing=self.study.target.framing,
                               competitor_label=self.study.target.competitor_label,
                               prompt_notes=self.profile.prompt_notes, rng=rng, seed=seed * 1000 + idx,
                               perception=self.profile.perception.model_dump())
            interrupted = False
            try:
                decision = await actor.decide(ctx)
            except LLMError as exc:
                state.fail("agent_failure", f"persona engine failed: {exc}", "technical_error",
                           step_idx=idx, screen=screen_id(url), shot=str(shot_path))
                return
            if not (decision.dismisses_popup or modal_visible(screen)):  # the page as people see it, not behind a pop-up
                self._audit(state, screen, url, cond, str(shot_path))
            if decision.action.kind == "quit":
                state.record_quit(idx, decision, screen, url, shot_path, frustration, state.sim_seconds)
                state.outcome, state.quit_reason = "quit", decision.quit_reason or "gave up"
                state.issues.extend(issues_from_step(state.steps[-1], decision, slow_threshold_s=self.settings.browser.slow_load_seconds,
                                                     history=state.steps[:-1]))
                return
            if decision.limitation or (unsupported_interaction(screen) and decision.action.kind == "wait"
                                       and memory.careful):
                state.fail("agent_failure", decision.limitation or "unsupported interaction",
                           "unsupported_interaction", step_idx=idx, screen=screen_id(url), shot=str(shot_path))
                return

            intended = decision.action
            executed, noisy = noise.apply(intended, browser.viewport)
            blocked = await self._guard(browser, executed)
            if blocked:
                state.fail("blocked", blocked, "blocked_by_safety", step_idx=idx, screen=screen_id(url),
                           shot=str(shot_path))
                return
            try:
                await browser.execute(executed, self.vault)
            except SafetyError as exc:
                state.fail("blocked", str(exc), "blocked_by_safety", step_idx=idx, screen=screen_id(url),
                           shot=str(shot_path))
                return
            except Exception as exc:
                state.fail("agent_failure", f"browser action failed: {exc}", "technical_error",
                           step_idx=idx, screen=screen_id(url), shot=str(shot_path))
                return
            max_wait = 1.0 if intended.kind == "scroll" else budget.wait_tolerance_s
            new_png, timing = await browser.wait_for_settle(screen.png, max_wait=max_wait)
            new_screen = await perceive_async(new_png)
            signals = compute_signals(prev=screen, cur=new_screen, decision=decision, executed=executed,
                                      timing=timing, pending_wait_s=pending_wait,
                                      slow_threshold_s=self.settings.browser.slow_load_seconds,
                                      history=state.steps, noise_applied=noisy, loading=browser.nav_pending,
                                      load_trigger=pending_trigger if pending_wait else "")
            if not timing.responded and browser.nav_pending:
                if not pending_wait:
                    pending_trigger = intended.target or intended.kind
                pending_wait += timing.settled_s
            else:
                pending_wait, pending_trigger = 0.0, ""
            frustration.update(signals)
            after_path = ""
            if signals.error_seen or signals.slow_load or signals.data_lost:
                after_path = str(shot_path.with_name(f"{idx:03d}_after.jpg"))
                _save_jpeg(new_screen.png, Path(after_path))
            state.effort += STEP_COST.get(intended.kind, 1.0)
            typing = len(intended.text or "") * 0.18 * skill_pace if intended.kind == "fill" else 0.0
            state.sim_seconds += self.settings.run.seconds_per_step * skill_pace + timing.settled_s + typing
            step = StepRecord(idx=idx, screenshot_path=str(shot_path), screen_hash=screen.dhash,
                              screen_id=screen_id(url), url=url, think=decision.think, intended=intended,
                              executed=executed, signals=signals, frustration=frustration.score,
                              sim_seconds=round(state.sim_seconds, 1), observations=decision.observations,
                              screen_id_after=screen_id(browser.url), after_screenshot=after_path)
            state.steps.append(step)
            state.issues.extend(issues_from_step(step, decision, slow_threshold_s=self.settings.browser.slow_load_seconds,
                                                 history=state.steps[:-1]))
            screen, last_signals, last_timing = new_screen, signals, timing
            if frustration.exceeded:
                causes = ", ".join(CAUSE_WORDS.get(c, c) for c in frustration.top_causes())
                state.outcome = "quit"
                state.quit_reason = f"That's it, I'm leaving: {causes}."
                state.quit_step = idx
                return
            if state.sim_seconds > budget.max_sim_seconds:
                state.outcome, state.quit_reason = "quit", "I've spent far too long on this; I'm giving up."
                state.quit_step = idx
                return
        if is_success(task, screen, browser.url):
            state.outcome = "completed"
            return
        state.outcome = "budget_exhausted"
        state.quit_reason = f"Ran out of patience after {len(state.steps)} actions without finishing."

    def _audit(self, state: _LoopState, screen: Screen, url: str, cond: dict[str, Any], shot: str) -> None:
        """Measured accessibility check, once per distinct screen and device (on the full-quality screenshot)."""
        from proofloop.web.a11y import check_screen

        device = cond.get("device") or "desktop"
        sid = screen_id(url)
        if any(s == sid and d == device and hash_distance(h, screen.dhash) <= 6 for s, d, h in self._a11y_seen):
            return
        self._a11y_seen.append((sid, device, screen.dhash))
        cfg = self.settings.device(device)
        state.a11y.extend(check_screen(screen, screen_id=sid, screenshot=shot, device=device, touch=cfg.mobile,
                                       scale=cfg.scale))

    async def _guard(self, browser: BrowserSession, action: Action) -> str | None:
        if action.kind in ("click", "fill") and action.x is not None:
            kind = "click"
            element = await browser.element_at(action.x, action.y)
        elif action.kind == "press" and (action.key or "").lower() == "enter":
            kind = "press_enter"
            element = await browser.element_at(0, 0, for_enter=True)
        else:
            return None
        result = self.guard.check(kind, element)
        return None if result.allowed else result.reason


class _LoopState:
    def __init__(self, session_id: str, started: float):
        self.session_id = session_id
        self.started = started
        self.steps: list[StepRecord] = []
        self.issues: list[IssueCandidate] = []
        self.outcome = "budget_exhausted"
        self.quit_reason: str | None = None
        self.quit_step: int | None = None
        self.sim_seconds = 0.0
        self.sim_interrupt_s = 0.0
        self.effort = 0.0
        self.a11y: list[Any] = []

    def fail(self, outcome: str, reason: str, category: str, *, step_idx: int = 0, screen: str = "",
             shot: str = "") -> None:
        self.outcome = outcome
        self.quit_reason = reason
        failure = "BLOCKED_BY_SAFETY" if category == "blocked_by_safety" else "AGENT_LIMITATION"
        self.issues.append(IssueCandidate(
            category=category, title=category.replace("_", " ").capitalize(), description=reason, severity=2,
            step_idx=step_idx, screen_id=screen, region=None, failure_class=failure,
            evidence={"screenshot": shot, "steps": [s.intended.describe() for s in self.steps]}))

    def record_quit(self, idx: int, decision: Decision, screen: Screen, url: str, shot: Path,
                    frustration: Frustration, sim: float) -> None:
        self.quit_step = idx
        self.steps.append(StepRecord(
            idx=idx, screenshot_path=str(shot), screen_hash=screen.dhash, screen_id=screen_id(url), url=url,
            think=decision.think, intended=decision.action, executed=None, signals=Signals(confusion=decision.confusion,
            cannot_find=any(o.kind == "cannot_find" for o in decision.observations)),
            frustration=frustration.score, sim_seconds=round(sim, 1), observations=decision.observations))


def navigation_issues(steps: list[StepRecord], outcome: str) -> list[IssueCandidate]:
    """For 'find' tasks: flag information buried several pages deep, or a path full of backtracking."""
    path: list[str] = []
    for s in steps:
        for sid in (s.screen_id, s.screen_id_after):
            if sid and (not path or path[-1] != sid):
                path.append(sid)
    pages = list(dict.fromkeys(path))
    backs = sum(1 for i in range(2, len(path)) if path[i] == path[i - 2])
    if len(pages) < DEEP_NAV_PAGES and backs < 2:
        return []
    found = outcome == "completed"
    route = " -> ".join(path[:8]) + (" ..." if len(path) > 8 else "")
    desc = (f"It took {len(pages)} different pages to {'find this' if found else 'look for this (not found)'}"
            + (f", going back {backs} time(s)" if backs else "") + f": {route}")
    last = steps[-1] if steps else None
    return [IssueCandidate(
        category="deep_navigation", title="Hard to find: it took many pages to get there", description=desc,
        severity=2 if found else 3, step_idx=last.idx if last else 0,
        screen_id=(last.screen_id_after or last.screen_id) if last else "", region=None,
        evidence={"screenshot": last.screenshot_path if last else "", "path": path,
                  "steps": [s.intended.describe() for s in steps]})]


def _dedupe(issues: list[IssueCandidate]) -> list[IssueCandidate]:
    """One candidate per (category, screen) per session; keep the most severe, count repeats."""
    best: dict[tuple[str, str], IssueCandidate] = {}
    counts: dict[tuple[str, str], int] = {}
    for issue in issues:
        key = (issue.category, issue.screen_id)
        counts[key] = counts.get(key, 0) + 1
        if key not in best or issue.severity > best[key].severity:
            best[key] = issue
    for key, issue in best.items():
        issue.evidence["occurrences"] = counts[key]
    return list(best.values())


def step_to_row(session_id: str, step: StepRecord) -> dict[str, Any]:
    return {"session_id": session_id, "idx": step.idx, "screenshot_path": step.screenshot_path,
            "screen_hash": str(step.screen_hash), "screen_id": step.screen_id, "url": step.url,
            "think_json": asdict(step.think), "action_json": step.executed.to_dict() if step.executed else None,
            "intended_json": step.intended.to_dict(), "signals_json": step.signals.to_dict(),
            "frustration": step.frustration, "sim_seconds": step.sim_seconds}


def empty_think() -> ThinkAloud:
    return ThinkAloud(sees="", expects="", does="", feels="")
