import asyncio

import pytest

from proofloop.core.cost import estimate_run, image_tokens
from proofloop.core.llm import FakeLLM, LLMError, LLMGateway, LLMRequest, validate_json

SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "integer", "minimum": 0}}}


def _settings(repo_settings, **roles):
    repo_settings.engine.roles = roles
    return repo_settings


def test_validate_json():
    assert validate_json({"answer": 3}, SCHEMA) == []
    assert validate_json({"answer": -1}, SCHEMA)
    assert validate_json({}, SCHEMA)
    assert validate_json({"answer": True}, SCHEMA)  # bool is not an integer


def test_cache_key_includes_seed():
    a = LLMRequest(role="actor", system="s", prompt="p", schema=SCHEMA, seed=1)
    b = LLMRequest(role="actor", system="s", prompt="p", schema=SCHEMA, seed=2)
    assert a.cache_key("fake", "m") != b.cache_key("fake", "m")
    assert a.cache_key("fake", "m") == LLMRequest(role="actor", system="s", prompt="p", schema=SCHEMA, seed=1).cache_key("fake", "m")


def test_gateway_caches_and_records_usage(settings, db):
    settings.engine.roles = {"judge": "fake"}
    fake = FakeLLM(lambda r: {"answer": 7})
    gw = LLMGateway(settings, db, clients={"fake": fake})
    req = LLMRequest(role="judge", system="s", prompt="p", schema=SCHEMA)
    first = asyncio.run(gw.complete(req))
    second = asyncio.run(gw.complete(req))
    assert first.data == second.data == {"answer": 7}
    assert second.cached and len(fake.calls) == 1
    assert len(db.all("SELECT * FROM llm_usage")) == 2


def test_gateway_retries_once_on_invalid_output(settings, db):
    settings.engine.roles = {"judge": "fake"}
    answers = iter([{"answer": "x"}, {"answer": 2}])
    fake = FakeLLM(lambda r: next(answers))
    gw = LLMGateway(settings, db, clients={"fake": fake})
    out = asyncio.run(gw.complete(LLMRequest(role="judge", system="s", prompt="p", schema=SCHEMA, cacheable=False)))
    assert out.data == {"answer": 2} and len(fake.calls) == 2
    assert "invalid" in fake.calls[1].prompt


def test_gateway_fails_after_second_invalid(settings):
    settings.engine.roles = {"judge": "fake"}
    gw = LLMGateway(settings, clients={"fake": FakeLLM(lambda r: {"nope": 1})})
    with pytest.raises(LLMError):
        asyncio.run(gw.complete(LLMRequest(role="judge", system="s", prompt="p", schema=SCHEMA)))


def test_heuristic_roles_never_call_a_model(settings):
    gw = LLMGateway(settings)
    with pytest.raises(LLMError):
        asyncio.run(gw.complete(LLMRequest(role="actor", system="s", prompt="p", schema=SCHEMA)))


def test_anthropic_engine_without_key_fails_clearly(settings, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    settings.engine.roles = {"judge": "anthropic"}
    gw = LLMGateway(settings)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY|anthropic"):
        asyncio.run(gw.complete(LLMRequest(role="judge", system="s", prompt="p", schema=SCHEMA)))


def test_cost_estimate_is_zero_for_heuristic_and_positive_for_claude(settings):
    free = estimate_run(settings, sessions=18, avg_steps=12, viewport=(1280, 800))
    assert free.total_usd == 0.0
    settings.engine.default = "anthropic"
    paid = estimate_run(settings, sessions=18, avg_steps=12, viewport=(1280, 800))
    assert paid.total_usd > 0
    assert "estimated total" in paid.describe()
    assert image_tokens(1280, 800) == int(1280 * 800 / 750)
