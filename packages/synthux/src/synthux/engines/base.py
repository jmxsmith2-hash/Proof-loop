"""The interface every persona engine implements."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from synthux.core.study import TaskSpec
from synthux.personas.models import Persona
from synthux.runner.behavior import PatienceBudget
from synthux.runner.memory import SessionMemory
from synthux.runner.perception import Screen
from synthux.runner.types import Decision, Signals, Timing


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


class Actor(Protocol):
    async def decide(self, ctx: ActorContext) -> Decision: ...
