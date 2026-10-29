"""Persona engines: the free heuristic engine and optional model-backed engines."""

from __future__ import annotations

from proofloop.core.llm import LLMGateway
from proofloop.personas.models import Persona
from proofloop.web.engines.base import Actor
from proofloop.web.engines.heuristic import HeuristicActor
from proofloop.web.engines.hybrid import HybridActor
from proofloop.web.engines.llm_actor import LLMActor


def actor_for(persona: Persona, gateway: LLMGateway) -> Actor:
    """The persona's own engine override wins; otherwise the configured actor engine."""
    engine = persona.engine or gateway.engine_for("actor")
    if engine == "heuristic":
        return HeuristicActor()
    if engine == "hybrid":
        hybrid = gateway.settings.engine.hybrid
        return HybridActor(gateway, engine=hybrid.engine, max_calls=hybrid.max_calls_per_session, model=persona.model)
    return LLMActor(gateway, model=persona.model, engine=persona.engine)
