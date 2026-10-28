"""Cheaper model use: smaller screenshots, prompt caching, effort per role, the hybrid persona."""

import asyncio
import io
import types

import pytest
from PIL import Image

from synthux.core.config import EngineConfig
from synthux.core.cost import call_cost, estimate_run
from synthux.core.llm import AnthropicClient, FakeLLM, LLMError, LLMGateway, LLMRequest
from synthux.engines import actor_for
from synthux.engines.hybrid import HybridActor
from synthux.engines.llm_actor import LLMActor
from synthux.personas.models import Persona, Traits
from synthux.runner.memory import SessionMemory
from synthux.runner.types import Signals
from tests.unit.runner.test_heuristic_actor import ctx, screen, tb


def png(w=1280, h=800):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def action(kind="click", x=512, y=320, **extra):
    return {"sees": "a page", "expects": "it works", "does": "I click", "feels": "fine", "confusion": 0.1,
            "action": {"kind": kind, "x": x, "y": y, "text": "", "key": "", "dy": 0, "target": "Go", **extra},
            "observations": [{"kind": "other", "detail": "x", "x": 100, "y": 50, "w": 200, "h": 40, "severity": 2}],
            "quit_reason": "", "limitation": ""}


def screen_ctx(texts, **kw):
    c = ctx(screen(texts), **kw)
    c.screen.png = png()
    return c


def test_model_sees_a_smaller_screenshot_and_coordinates_map_back(settings):
    settings.engine.roles = {"actor": "fake"}
    fake = FakeLLM(lambda r: action())
    actor = LLMActor(LLMGateway(settings, clients={"fake": fake}))
    d = asyncio.run(actor.decide(screen_ctx([tb("Go", 600, 380, fill=True)])))
    req = fake.calls[0]
    assert req.images[0][:3] == b"\xff\xd8\xff" and req.image_size == (1024, 640)
    assert Image.open(io.BytesIO(req.images[0])).size == (1024, 640)
    assert (d.action.x, d.action.y) == (640, 400)                   # 512/0.8, 320/0.8
    assert d.observations[0].region == {"x": 125, "y": 62, "w": 250, "h": 50}
    assert "1024x640" in req.prompt


def test_persona_text_is_a_stable_cached_system_prompt(settings):
    settings.engine.roles = {"actor": "fake"}
    fake = FakeLLM(lambda r: action())
    actor = LLMActor(LLMGateway(settings, clients={"fake": fake}))
    mem = SessionMemory()
    for step in range(2):
        c = screen_ctx([tb("Go", 600, 380, fill=True)], mem=mem)
        c.step = step
        asyncio.run(actor.decide(c))
    a, b = fake.calls
    assert a.cache_system and a.system == b.system and "Your goal" in a.system
    assert a.prompt != b.prompt and "Step 1" in a.prompt and "Step 2" in b.prompt


def test_effort_comes_from_models_yaml(settings):
    settings.engine.roles = {"actor": "fake"}
    settings.models["fake"] = settings.models["anthropic"].model_copy(update={"effort": {"actor": "low"}})
    fake = FakeLLM(lambda r: {"answer": 1})
    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "integer"}}}
    asyncio.run(LLMGateway(settings, clients={"fake": fake}).complete(
        LLMRequest(role="actor", system="s", prompt="p", schema=schema)))
    assert fake.calls[0].effort == "low"


def test_cached_input_is_billed_at_a_fraction(settings):
    full = call_cost(settings, "anthropic", "claude-opus-5-5", 1000, 0)
    cached = call_cost(settings, "anthropic", "claude-opus-5-5", 0, 0, cache_read=1000)
    assert cached == pytest.approx(full * 0.1)


def test_anthropic_request_shape(monkeypatch):
    sent = {}

    class Messages:
        async def create(self, **kw):
            sent.update(kw)
            usage = types.SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=900,
                                          cache_creation_input_tokens=0)
            return types.SimpleNamespace(stop_reason="end_turn", usage=usage,
                                         content=[types.SimpleNamespace(type="text", text='{"answer": 1}')])

    client = AnthropicClient.__new__(AnthropicClient)
    client._anthropic = types.SimpleNamespace(RateLimitError=KeyError, APIStatusError=KeyError,
                                              APIConnectionError=KeyError)
    client._client = types.SimpleNamespace(messages=Messages())
    jpeg = io.BytesIO()
    Image.new("RGB", (8, 8)).save(jpeg, "JPEG")
    req = LLMRequest(role="actor", system="persona", prompt="step", schema={"type": "object"}, max_tokens=50,
                     images=[jpeg.getvalue()], cache_system=True, effort="low")
    out = asyncio.run(client.complete(req, "claude-haiku-5-5"))
    assert sent["max_tokens"] >= 8000                          # thinking needs room; billing is per use
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["output_config"]["effort"] == "low"
    assert sent["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"
    assert "temperature" not in sent
    assert out.cache_read_tokens == 900


def test_hybrid_is_only_for_the_actor():
    EngineConfig(roles={"actor": "hybrid"})
    with pytest.raises(ValueError):
        EngineConfig(roles={"judge": "hybrid"})
    with pytest.raises(ValueError):
        EngineConfig(default="hybrid")


def hybrid(settings, responder, max_calls=6):
    settings.engine.roles = {"actor": "hybrid"}
    settings.engine.hybrid.engine = "fake"
    settings.engine.hybrid.max_calls_per_session = max_calls
    fake = FakeLLM(responder)
    persona = Persona(id="p", label="p", traits=Traits(), narrative="")
    actor = actor_for(persona, LLMGateway(settings, clients={"fake": fake}))
    assert isinstance(actor, HybridActor)
    return actor, fake


def test_hybrid_uses_rules_for_routine_screens(settings):
    actor, fake = hybrid(settings, lambda r: action())
    d = asyncio.run(actor.decide(screen_ctx([tb("Welcome", 100, 100, h=30), tb("Continue", 300, 400, fill=True)])))
    assert d.action.target == "Continue" and not fake.calls


def test_hybrid_asks_the_model_when_the_rules_are_stuck(settings):
    actor, fake = hybrid(settings, lambda r: action(target="Hidden link"))
    texts = [tb("Nothing useful here", 100, 100)]
    # Already searched this screen: down to the bottom, a careful look, back to the top.
    mem = SessionMemory(screen_sig=screen(texts).signature(), scrolls=3, at_bottom=True, careful=True,
                        scrolled_up=True, last_action_kind="scroll")
    d = asyncio.run(actor.decide(screen_ctx(texts, mem=mem)))
    assert len(fake.calls) == 1 and "unclear" in fake.calls[0].prompt
    assert d.action.kind == "click" and mem.last_action_kind == "click" and mem.clicks[-1]["text"] == "Hidden link"


def test_hybrid_respects_its_cap_and_survives_model_errors(settings):
    def boom(r):
        raise LLMError("down")
    actor, fake = hybrid(settings, boom, max_calls=1)
    c = lambda: screen_ctx([tb("Continue", 300, 400, fill=True)], last=Signals(no_change=True))  # noqa: E731
    first = asyncio.run(actor.decide(c()))
    second = asyncio.run(actor.decide(c()))
    assert first.action.kind and second.action.kind      # the rules' decision is used when the model fails
    assert actor.calls == 1


def test_estimate_is_far_lower_with_the_hybrid_persona(settings):
    settings.engine.roles = {"actor": "anthropic"}
    full = estimate_run(settings, sessions=100, avg_steps=8, viewport=(1280, 800)).total_usd
    settings.engine.roles = {"actor": "hybrid"}
    hyb = estimate_run(settings, sessions=100, avg_steps=8, viewport=(1280, 800)).total_usd
    assert 0 < hyb < full * 0.5
