"""Model-backed persona (Claude or a local Ollama model). Optional; costs money with Claude.

The model receives the screenshot and the persona description, never the page
HTML. It answers with one action at pixel coordinates plus its think-aloud.
"""

from __future__ import annotations

import json
from importlib import resources

from synthux.core.llm import LLMGateway, LLMRequest
from synthux.engines.base import ActorContext
from synthux.runner.types import Action, Decision, Observation, ThinkAloud

ACTION_SCHEMA = {
    "type": "object",
    "required": ["sees", "expects", "does", "feels", "confusion", "action", "observations", "quit_reason",
                 "limitation"],
    "properties": {
        "sees": {"type": "string"},
        "expects": {"type": "string"},
        "does": {"type": "string"},
        "feels": {"type": "string"},
        "confusion": {"type": "number", "minimum": 0, "maximum": 1},
        "action": {
            "type": "object",
            "required": ["kind", "x", "y", "text", "key", "dy", "target"],
            "properties": {
                "kind": {"type": "string", "enum": ["click", "fill", "press", "scroll", "wait", "back", "quit"]},
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "text": {"type": "string"}, "key": {"type": "string"},
                "dy": {"type": "integer"}, "target": {"type": "string"},
            },
        },
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "detail", "x", "y", "w", "h", "severity"],
                "properties": {
                    "kind": {"type": "string", "enum": ["ambiguous_label", "unclear_input_format",
                                                        "data_lost_on_error", "unexplained_error",
                                                        "hidden_control", "cannot_find", "other"]},
                    "detail": {"type": "string"},
                    "x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer"},
                    "h": {"type": "integer"}, "severity": {"type": "integer", "minimum": 1, "maximum": 4},
                },
            },
        },
        "quit_reason": {"type": "string"},
        "limitation": {"type": "string"},
    },
}


def _prompt_template() -> str:
    return (resources.files("synthux") / "prompts" / "actor.md").read_text(encoding="utf-8")


def build_prompt(ctx: ActorContext) -> str:
    t = ctx.persona.traits
    inputs = {k: v for k, v in ctx.task.inputs.items()}  # credentials stay as {{cred:name}} placeholders
    product = ctx.competitor_label if ctx.framing == "competitor" else "this product"
    memory = ctx.memory.facts[-8:]
    last = ctx.last_signals.active() if ctx.last_signals else []
    return _prompt_template().format(
        narrative=ctx.persona.narrative, traits=t.model_dump_json(), goal=ctx.task.goal, product=product,
        inputs=json.dumps(inputs), memory="\n".join(f"- {m}" for m in memory) or "- (nothing yet)",
        step=ctx.step + 1, max_steps=ctx.budget.max_steps, last=", ".join(last) or "none",
        busy="yes" if ctx.browser_busy else "no", interrupted="yes" if ctx.interrupted else "no",
        width=ctx.screen.width, height=ctx.screen.height,
        notes="\n".join(f"- {n}" for n in ctx.prompt_notes) or "- none",
    )


class LLMActor:
    def __init__(self, gateway: LLMGateway, model: str | None = None, engine: str | None = None):
        self.gateway = gateway
        self.model = model
        self.engine = engine

    async def decide(self, ctx: ActorContext) -> Decision:
        request = LLMRequest(role="actor", system="You are role-playing a specific person testing a website. "
                                                   "Behave exactly as that person would, including their limits.",
                             prompt=build_prompt(ctx), schema=ACTION_SCHEMA, images=[ctx.screen.png],
                             image_size=(ctx.screen.width, ctx.screen.height), seed=ctx.seed,
                             max_tokens=1200, model=self.model, engine=self.engine)
        data = (await self.gateway.complete(request)).data
        a = data["action"]
        action = Action(kind=a["kind"], x=_clamp(a.get("x"), ctx.screen.width), y=_clamp(a.get("y"), ctx.screen.height),
                        text=a.get("text") or None, key=a.get("key") or None, dy=int(a.get("dy") or 0),
                        target=a.get("target", ""))
        if action.kind == "fill" and not action.text:
            action.kind = "click"
        observations = [Observation(o["kind"], o["detail"], {"x": o["x"], "y": o["y"], "w": o["w"], "h": o["h"]},
                                    int(o["severity"])) for o in data.get("observations", [])]
        ctx.memory.remember(f"step {ctx.step + 1}: {data['does'][:120]}")
        return Decision(action=action, think=ThinkAloud(data["sees"], data["expects"], data["does"], data["feels"]),
                        confusion=float(data["confusion"]), observations=observations,
                        quit_reason=data.get("quit_reason") or None, limitation=data.get("limitation") or None)


def _clamp(value: int | None, limit: int) -> int | None:
    if value is None:
        return None
    return max(0, min(limit - 1, int(value)))
