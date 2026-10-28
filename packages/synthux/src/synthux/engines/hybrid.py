"""Hybrid persona: the free rule engine acts, and a model is asked only when a screen is hard.

Routine steps (closing a cookie banner, typing into labelled fields, clicking an
obvious button) are decided by the rules at no cost. A model sees the screenshot
only when the rules are stuck or unsure: searching for a way forward, about to
quit, meeting an error they can't explain, an unclear label, a click that did
nothing. A per-session cap bounds the cost.
"""

from __future__ import annotations

from synthux.core.llm import LLMError, LLMGateway
from synthux.core.logging import get_logger
from synthux.engines.base import ActorContext
from synthux.engines.heuristic import HeuristicActor
from synthux.engines.llm_actor import LLMActor
from synthux.runner.types import Decision

log = get_logger(__name__)

HARD_OBSERVATIONS = {"cannot_find", "ambiguous_label", "unexplained_error", "dead_end", "unclear_input_format"}


def escalation_reason(ctx: ActorContext, decision: Decision) -> str | None:
    """Why the rules' decision should be checked by a model, or None if it is routine."""
    action = decision.action
    if decision.limitation:
        return "the rules cannot use this screen"
    if action.kind == "quit":
        return "the rules are about to give up"
    hard = sorted({o.kind for o in decision.observations} & HARD_OBSERVATIONS)
    if hard:
        return "the rules flagged " + ", ".join(hard)
    if action.kind in ("scroll", "wait") and ctx.memory.scrolls >= 2:
        return "the rules are searching for a way forward"
    if action.kind == "back":
        return "the rules hit a dead end"
    if ctx.last_signals and ctx.last_signals.no_change and action.kind == "click":
        return "the last click did nothing"
    if decision.confusion >= 0.6:
        return "the rules are unsure"
    return None


class HybridActor:
    def __init__(self, gateway: LLMGateway, *, engine: str, max_calls: int, model: str | None = None):
        self.rules = HeuristicActor()
        self.model = LLMActor(gateway, model=model, engine=engine)
        self.max_calls = max_calls
        self.calls = 0
        self.escalations: list[tuple[int, str]] = []

    async def decide(self, ctx: ActorContext) -> Decision:
        decision = await self.rules.decide(ctx)
        reason = escalation_reason(ctx, decision)
        if reason is None or self.calls >= self.max_calls:
            return decision
        self.calls += 1
        hint = (f"(The obvious next step here was unclear: {reason}. Look at the whole screen as this person "
                "would before deciding.)")
        try:
            better = await self.model.decide(ctx, hint=hint)
        except LLMError as exc:
            log.warning("hybrid: model call failed (%s); keeping the rules' decision", exc)
            return decision
        self.escalations.append((ctx.step, reason))
        _sync_memory(ctx, decision, better)
        # What the rules measured on this screen (pop-ups, unclear labels) stays; the model adds its own notes.
        seen = {(o.kind, o.detail) for o in better.observations}
        better.observations = [o for o in decision.observations if (o.kind, o.detail) not in seen] + \
            better.observations
        return better


def _sync_memory(ctx: ActorContext, ours: Decision, theirs: Decision) -> None:
    """The rules already recorded their own choice in memory; replace it with what is actually done."""
    mem = ctx.memory
    if ours.action.kind == "click" and mem.clicks and mem.clicks[-1].get("text") == ours.action.target:
        mem.clicks.pop()
    mem.last_action_kind = theirs.action.kind
    mem.popup_pending = ""
    mem.target_pending = None
    if theirs.action.kind == "click" and theirs.action.x is not None:
        mem.clicks.append({"x": theirs.action.x, "y": theirs.action.y, "text": theirs.action.target,
                           "sig": mem.screen_sig})
