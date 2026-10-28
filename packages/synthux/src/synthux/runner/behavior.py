"""Patience budget, frustration score, and realistic noise (misclicks and typos)."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from synthux.calibration.profiles import CalibrationProfile
from synthux.core.config import NoiseConfig
from synthux.personas.models import Traits
from synthux.runner.types import Action, Signals

# ---------------------------------------------------------------- patience


@dataclass
class PatienceBudget:
    max_steps: int
    max_retries: int          # attempts at the same target or the same form before giving up
    max_sim_seconds: float
    wait_tolerance_s: float   # how long the persona waits for a screen to react before acting again

    @classmethod
    def for_persona(cls, traits: Traits, profile: CalibrationProfile, *, time_pressure: bool,
                    task_cap: int | None = None) -> "PatienceBudget":
        p = profile.patience
        steps = max(p.min_steps, p.base_steps + (traits.patience - 3) * p.steps_per_patience_point)
        seconds = float(p.seconds_by_patience.get(traits.patience, 240))
        retries = int(p.retries_by_patience.get(traits.patience, 3))
        if time_pressure:
            steps = max(p.min_steps, int(steps * p.time_pressure_factor))
            seconds *= p.time_pressure_factor
            retries = max(1, int(round(retries * p.time_pressure_factor)))
        if task_cap:
            steps = min(steps, task_cap)
        wait = {1: 3.0, 2: 4.0, 3: 6.0, 4: 8.0, 5: 10.0}[traits.patience] * (0.75 if time_pressure else 1.0)
        return cls(max_steps=steps, max_retries=retries, max_sim_seconds=seconds, wait_tolerance_s=wait)


# ------------------------------------------------------------- frustration


@dataclass
class Frustration:
    """A running score that rises with measured problems; crossing the threshold means quitting."""

    threshold: float
    multiplier: float
    weights: dict[str, float]
    relief: float
    slow_threshold_s: float
    score: float = 0.0
    timeline: list[float] = field(default_factory=list)
    contributions: dict[str, float] = field(default_factory=dict)

    @classmethod
    def for_persona(cls, traits: Traits, profile: CalibrationProfile, *, time_pressure: bool,
                    slow_threshold_s: float) -> "Frustration":
        f = profile.frustration
        threshold = float(f.threshold_by_patience.get(traits.patience, 65))
        if time_pressure:
            threshold *= 0.85
        return cls(threshold=threshold, multiplier=float(f.mood_multiplier.get(traits.mood, 1.0)),
                   weights=dict(f.weights), relief=f.progress_relief, slow_threshold_s=slow_threshold_s)

    def update(self, signals: Signals) -> float:
        delta = 0.0
        w = self.weights
        parts: dict[str, float] = {}
        for name in ("error_seen", "unexplained_error", "no_change", "repeat_action", "backtrack",
                     "data_lost", "cannot_find", "scroll_search", "no_loading_feedback"):
            if getattr(signals, name):
                parts[name] = w.get(name, 0.0)
        if signals.slow_load:
            over = max(0.0, signals.load_seconds - self.slow_threshold_s)
            parts["slow_load"] = w.get("slow_load", 0.0) + over * w.get("slow_load_per_second", 0.0)
        if signals.confusion > 0:
            parts["confusion"] = w.get("confusion", 0.0) * signals.confusion
        for name, value in parts.items():
            value *= self.multiplier
            self.contributions[name] = self.contributions.get(name, 0.0) + value
            delta += value
        if signals.progress and not parts:
            delta -= self.relief
        self.score = max(0.0, self.score + delta)
        self.timeline.append(round(self.score, 2))
        return self.score

    @property
    def exceeded(self) -> bool:
        return self.score >= self.threshold

    def top_causes(self, n: int = 2) -> list[str]:
        return [k for k, _ in sorted(self.contributions.items(), key=lambda kv: -kv[1])[:n]]


# -------------------------------------------------------------------- noise

KEYBOARD_NEIGHBOURS = {
    "a": "qwsz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr", "f": "drtgvc", "g": "ftyhbv",
    "h": "gyujnb", "i": "ujko", "j": "huikmn", "k": "jiolm", "l": "kop", "m": "njk", "n": "bhjm",
    "o": "iklp", "p": "ol", "q": "wa", "r": "edft", "s": "awedxz", "t": "rfgy", "u": "yhji",
    "v": "cfgb", "w": "qase", "x": "zsdc", "y": "tghu", "z": "asx",
    "1": "2q", "2": "13w", "3": "24e", "4": "35r", "5": "46t", "6": "57y", "7": "68u", "8": "79i", "9": "80o", "0": "9p",
}
MOTOR_FACTOR = {"none": 1.0, "tremor": 2.5, "limited_dexterity": 2.0}


class Noise:
    """Injects misclicks and typos; rates depend on skill, motor traits and device."""

    def __init__(self, config: NoiseConfig, traits: Traits, rng: random.Random):
        self.config = config
        self.traits = traits
        self.rng = rng
        motor = MOTOR_FACTOR[traits.motor]
        device = 1.3 if traits.device == "mobile" else 1.0
        hurry = 1.3 if traits.time_pressure else 1.0
        self.misclick_rate = min(0.6, config.misclick_rate_by_skill.get(traits.tech_skill, 0.05) * motor * device * hurry)
        self.typo_rate = min(0.3, config.typo_rate_by_skill.get(traits.tech_skill, 0.02) * (1.5 if motor > 1 else 1.0) * hurry)
        self.sigma = config.misclick_sigma_px * motor * device

    def apply(self, action: Action, viewport: tuple[int, int]) -> tuple[Action, bool]:
        """Return (executed action, whether noise changed it). Credentials are never altered."""
        if not self.config.enabled or action.kind not in ("click", "fill"):
            return action, False
        executed = Action(**action.__dict__)
        changed = False
        if action.x is not None and action.y is not None:
            if self.rng.random() < self.misclick_rate:
                executed.x = int(min(viewport[0] - 1, max(0, action.x + self.rng.gauss(0, self.sigma * 2.2))))
                executed.y = int(min(viewport[1] - 1, max(0, action.y + self.rng.gauss(0, self.sigma * 1.6))))
                changed = (executed.x, executed.y) != (action.x, action.y)
            else:  # ordinary pointing jitter
                executed.x = int(action.x + self.rng.gauss(0, 1.5))
                executed.y = int(action.y + self.rng.gauss(0, 1.5))
        if action.kind == "fill" and action.text and "{{cred:" not in action.text:
            typed = self.typo(action.text)
            if typed != action.text:
                executed.text, changed = typed, True
        return executed, changed

    def typo(self, text: str) -> str:
        out = []
        for ch in text:
            if ch.isalnum() and self.rng.random() < self.typo_rate:
                choice = self.rng.random()
                low = ch.lower()
                if choice < 0.5 and low in KEYBOARD_NEIGHBOURS:
                    out.append(self.rng.choice(KEYBOARD_NEIGHBOURS[low]))
                elif choice < 0.75:
                    continue  # dropped character
                else:
                    out.append(ch + ch)  # doubled character
            else:
                out.append(ch)
        return "".join(out)
