"""Persona engines: the free heuristic engine and optional model-backed engines."""

from __future__ import annotations

from synthux.core.llm import LLMGateway
from synthux.engines.base import Actor
from synthux.engines.heuristic import HeuristicActor
from synthux.engines.hybrid import HybridActor
from synthux.engines.llm_actor import LLMActor
from synthux.personas.models import Persona


def actor_for(persona: Persona, gateway: LLMGateway) -> Actor:
    """The persona's own engine override wins; otherwise the configured actor engine."""
    engine = persona.engine or gateway.engine_for("actor")
    if engine == "heuristic":
        return HeuristicActor()
    if engine == "hybrid":
        hybrid = gateway.settings.engine.hybrid
        return HybridActor(gateway, engine=hybrid.engine, max_calls=hybrid.max_calls_per_session, model=persona.model)
    return LLMActor(gateway, model=persona.model, engine=persona.engine)
