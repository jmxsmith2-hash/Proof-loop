"""Model-backed persona (Claude or a local Ollama model). Optional; costs money with Claude.

The model receives the screenshot and the persona description, never the page
HTML. It answers with one action at pixel coordinates plus its think-aloud.
"""

from __future__ import annotations

import io
import json
from importlib import resources

from PIL import Image

from proofloop.core.llm import LLMGateway, LLMRequest
from proofloop.web.engines.base import ActorContext
from proofloop.web.runner.types import Action, Decision, Observation, ThinkAloud

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


def _prompt_parts() -> tuple[str, str]:
    text = (resources.files("proofloop.web") / "prompts" / "actor.md").read_text(encoding="utf-8")
    stable, step = text.split("---STEP---", 1)
    return stable.strip(), step.strip()


def build_prompt(ctx: ActorContext, size: tuple[int, int] | None = None, hint: str = "") -> tuple[str, str]:
    """(system, step prompt). The system part is identical on every step of a session, so it is cached."""
    t = ctx.persona.traits
    inputs = {k: v for k, v in ctx.task.inputs.items()}  # credentials stay as {{cred:name}} placeholders
    product = ctx.competitor_label if ctx.framing == "competitor" else "this product"
    memory = ctx.memory.facts[-8:]
    last = ctx.last_signals.active() if ctx.last_signals else []
    width, height = size or (ctx.screen.width, ctx.screen.height)
    stable, step = _prompt_parts()
    system = stable.format(
        narrative=ctx.persona.narrative, traits=t.model_dump_json(), goal=ctx.task.goal, product=product,
        inputs=json.dumps(inputs, sort_keys=True), notes="\n".join(f"- {n}" for n in ctx.prompt_notes) or "- none")
    prompt = step.format(
        memory="\n".join(f"- {m}" for m in memory) or "- (nothing yet)",
        step=ctx.step + 1, max_steps=ctx.budget.max_steps, last=", ".join(last) or "none",
        busy="yes" if ctx.browser_busy else "no", interrupted="yes" if ctx.interrupted else "no",
        hint=hint, width=width, height=height)
    return system, prompt


def model_image(png: bytes, width: int, max_width: int) -> tuple[bytes, float]:
    """Scale a screenshot down for the model (image tokens grow with pixels). Returns (image, scale factor)."""
    if width <= max_width:
        return png, 1.0
    image = Image.open(io.BytesIO(png)).convert("RGB")
    scale = max_width / image.width
    small = image.resize((max_width, round(image.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    return buf.getvalue(), scale


class LLMActor:
    def __init__(self, gateway: LLMGateway, model: str | None = None, engine: str | None = None):
        self.gateway = gateway
        self.model = model
        self.engine = engine

    async def decide(self, ctx: ActorContext, hint: str = "") -> Decision:
        max_width = self.gateway.settings.engine.model_image_width
        image, scale = model_image(ctx.screen.png, ctx.screen.width, max_width)
        size = (round(ctx.screen.width * scale), round(ctx.screen.height * scale))
        system, prompt = build_prompt(ctx, size, hint)
        request = LLMRequest(role="actor", system=system, prompt=prompt, schema=ACTION_SCHEMA, images=[image],
                             image_size=size, seed=ctx.seed, max_tokens=4000, model=self.model,
                             engine=self.engine, cache_system=True)
        data = (await self.gateway.complete(request)).data
        a = data["action"]

        def full(v: int | None, limit: int) -> int | None:  # model coordinates -> screen coordinates
            return None if v is None else _clamp(round(int(v) / scale), limit)

        action = Action(kind=a["kind"], x=full(a.get("x"), ctx.screen.width), y=full(a.get("y"), ctx.screen.height),
                        text=a.get("text") or None, key=a.get("key") or None, dy=round(int(a.get("dy") or 0) / scale),
                        target=a.get("target", ""))
        if action.kind == "fill" and not action.text:
            action.kind = "click"
        observations = [Observation(o["kind"], o["detail"],
                                    {"x": round(o["x"] / scale), "y": round(o["y"] / scale),
                                     "w": round(o["w"] / scale), "h": round(o["h"] / scale)}, int(o["severity"]))
                        for o in data.get("observations", [])]
        ctx.memory.remember(f"step {ctx.step + 1}: {data['does'][:120]}")
        return Decision(action=action, think=ThinkAloud(data["sees"], data["expects"], data["does"], data["feels"]),
                        confusion=float(data["confusion"]), observations=observations,
                        quit_reason=data.get("quit_reason") or None, limitation=data.get("limitation") or None)


def _clamp(value: int | None, limit: int) -> int | None:
    if value is None:
        return None
    return max(0, min(limit - 1, int(value)))
