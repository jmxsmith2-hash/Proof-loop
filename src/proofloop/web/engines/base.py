"""The interface every persona engine implements."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from proofloop.personas.models import Persona
from proofloop.web.runner.behavior import PatienceBudget
from proofloop.web.runner.memory import SessionMemory
from proofloop.web.runner.perception import Screen
from proofloop.web.runner.types import Decision, Signals, Timing
from proofloop.web.study import TaskSpec


@dataclass
class ActorContext:
    persona: Persona
    task: TaskSpec
    screen: Screen                 # pixels and what OCR could read from them, nothing else
    memory: SessionMemory
    budget: PatienceBudget
    step: int
    frustration: float
    frustration_threshold: float
    last_signals: Signals | None
    last_timing: Timing | None
    browser_busy: bool             # the browser's own loading indicator (tab spinner) is visible
    interrupted: bool              # the persona just came back from an interruption
    framing: str                   # neutral | competitor
    competitor_label: str
    prompt_notes: list[str]
    rng: random.Random
    seed: int
    perception: dict | None = None  # calibrated contrast thresholds (heuristic engine)


class Actor(Protocol):
    async def decide(self, ctx: ActorContext) -> Decision: ...
