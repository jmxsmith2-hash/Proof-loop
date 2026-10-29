"""Persona generation: data -> anonymized evidence -> spread traits -> narrative -> diversity check."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from proofloop.core.db import Database, new_id, utcnow
from proofloop.core.llm import LLMGateway, LLMRequest
from proofloop.core.logging import get_logger
from proofloop.personas.anonymize import anonymize_text
from proofloop.personas.design import design_traits, replacement_traits
from proofloop.personas.diversity import DESCRIPTION_THRESHOLD, description_pairs
from proofloop.personas.ingest import SourceRecord, load_sources
from proofloop.personas.models import Persona, Traits

log = get_logger(__name__)

SKILL = {1: ["rarely uses apps and finds most websites confusing", "avoids technology unless a family member helps"],
         2: ["uses a few familiar apps but gets lost on new sites", "can shop online but distrusts unfamiliar forms"],
         3: ["is an everyday internet user", "handles most websites without help"],
         4: ["is comfortable with technology and tries shortcuts", "picks up new apps quickly"],
         5: ["is a power user who expects keyboard shortcuts and fast flows", "works in tech and notices sloppy design"]}
PATIENCE = {1: ["give up at the first real obstacle", "abandon anything that wastes their time"],
            2: ["tolerate one or two problems before leaving", "get irritated quickly by slow pages"],
            3: ["will retry a couple of times before giving up", "are reasonably patient"],
            4: ["keep trying different approaches", "stay patient as long as there is visible progress"],
            5: ["will persist for a long time to finish", "read help text and try everything"]}
FAMILIARITY = {1: "has never used a product like this", 2: "has seen similar products but rarely uses them",
               3: "has used a few similar products", 4: "regularly uses similar products",
               5: "has used competing products for years"}
MOOD = {"calm": "relaxed", "distracted": "often interrupted and distracted", "stressed": "stressed and short on attention"}
DEVICE = {"desktop": "a laptop or desktop", "mobile": "a phone", "tablet": "a tablet"}
ACCESS = {"low_vision": "have low vision and struggle with small or low-contrast text",
          "color_blind": "are colour blind and miss colour-only cues",
          "tremor": "have a hand tremor that makes small targets hard to hit",
          "limited_dexterity": "have limited dexterity and mis-tap small controls"}

NARRATIVE_SCHEMA = {
    "type": "object",
    "required": ["label", "narrative"],
    "properties": {"label": {"type": "string"}, "narrative": {"type": "string"}},
}


def _label(t: Traits) -> str:
    parts = []
    if t.time_pressure:
        parts.append("Hurried")
    elif t.mood == "distracted":
        parts.append("Distracted")
    elif t.patience >= 4:
        parts.append("Persistent")
    elif t.patience <= 2:
        parts.append("Impatient")
    else:
        parts.append("Steady")
    parts.append(t.device)
    if t.eyesight != "normal" or t.motor != "none":
        parts.append({"low_vision": "low-vision", "color_blind": "colour-blind"}.get(t.eyesight, "motor-impaired"))
    parts.append({1: "novice", 2: "casual", 3: "everyday", 4: "confident", 5: "expert"}[t.tech_skill])
    parts.append("first-timer" if t.familiarity <= 2 else "regular" if t.familiarity >= 4 else "user")
    return " ".join(parts).capitalize()


def template_narrative(t: Traits, quotes: list[str], rng: random.Random) -> str:
    lines = [
        f"Aged {t.age_range}, uses {DEVICE[t.device]}. Usually {MOOD[t.mood]}"
        + (" and in a hurry." if t.time_pressure else "."),
        f"This person {rng.choice(SKILL[t.tech_skill])} and {FAMILIARITY[t.familiarity]}.",
        f"They {rng.choice(PATIENCE[t.patience])}.",
        f"Budget is {t.budget}" + (f" (around ${t.budget_usd:.0f})." if t.budget_usd else "."),
    ]
    for key in (t.eyesight, t.motor):
        if key in ACCESS:
            lines.append(f"They {ACCESS[key]}.")
    if t.language != "native":
        lines.append("English is their second language; idioms and jargon slow them down.")
    if t.hand_size != "medium" or t.grip_strength != "normal":
        lines.append(f"Hands: {t.hand_size}, grip strength {t.grip_strength}.")
    lines.append(f"Goal: {t.goal}.")
    if quotes:
        lines.append(f'Grounded in real feedback such as: "{quotes[0]}"')
    return " ".join(lines)


def _quotes(records: list[SourceRecord], limit: int = 3) -> list[str]:
    out = []
    for r in records:
        if r.text:
            out.append(r.text[:220])
        if len(out) >= limit:
            break
    return out


class PersonaGenerator:
    def __init__(self, gateway: LLMGateway | None = None, *, seed: int = 7,
                 threshold: float = DESCRIPTION_THRESHOLD, max_regenerations: int = 10):
        self.gateway = gateway
        self.seed = seed
        self.threshold = threshold
        self.max_regenerations = max_regenerations
        self.regenerated: list[dict[str, Any]] = []

    # --------------------------------------------------------------- public
    async def generate(self, count: int, sources: list[Path | str] | None = None,
                       manual: list[dict[str, Any]] | None = None,
                       engines: list[str] | None = None, include_accessibility: bool = True) -> list[Persona]:
        records, report = load_sources(list(sources or []))
        personas = [self._manual(m) for m in (manual or [])]
        remaining = max(0, count - len(personas))
        if remaining:
            if not records and not manual:
                log.info("no source data given; personas come from the trait grid only")
            designed = design_traits(remaining, records, seed=self.seed,
                                     include_accessibility=include_accessibility)
            for i, (traits, recs) in enumerate(designed):
                personas.append(await self._build(traits, recs, i))
        personas = await self._enforce_diversity(personas)
        self._assign_engines(personas, engines or [])
        log.info("generated %d personas (%d regenerated for diversity, %d PII items removed)",
                 len(personas), len(self.regenerated), report.total)
        return personas

    # --------------------------------------------------------------- building
    def _manual(self, spec: dict[str, Any]) -> Persona:
        spec = dict(spec)
        label = spec.pop("label", None)
        narrative = spec.pop("narrative", None)
        traits = Traits.model_validate(spec)
        rng = random.Random(self.seed)
        return Persona(id=new_id("per"), label=label or _label(traits), traits=traits,
                       narrative=anonymize_text(narrative) if narrative else template_narrative(traits, [], rng),
                       source_summary="manual definition")

    async def _build(self, traits: Traits, records: list[SourceRecord], index: int, generation: int = 1) -> Persona:
        rng = random.Random(self.seed * 1000 + index * 17 + generation)
        quotes = _quotes(records)
        label, narrative = _label(traits), template_narrative(traits, quotes, rng)
        if self.gateway is not None and self.gateway.is_model_backed("persona"):
            label, narrative = await self._llm_narrative(traits, quotes, index + generation * 100)
        files = sorted({r.source_file for r in records})
        summary = f"{len(records)} records from {', '.join(files)}" if records else "trait grid (no source records)"
        return Persona(id=new_id("per"), label=label, traits=traits, narrative=narrative,
                       source_summary=summary, quotes=quotes, generation=generation)

    async def _llm_narrative(self, traits: Traits, quotes: list[str], seed: int) -> tuple[str, str]:
        assert self.gateway is not None
        prompt = (
            "Write a short, concrete persona for usability testing. Keep every trait value exactly as given; "
            "do not invent a name, employer or any identifying detail. 80-120 words, third person, plain language.\n"
            f"Traits: {traits.model_dump_json()}\n"
            "Anonymized quotes from real users this persona is based on:\n- " + "\n- ".join(quotes or ["(none)"])
        )
        response = await self.gateway.complete(LLMRequest(
            role="persona", system="You write realistic, specific user personas grounded in research data.",
            prompt=prompt, schema=NARRATIVE_SCHEMA, seed=seed, max_tokens=800))
        return response.data["label"][:80], anonymize_text(response.data["narrative"])

    # --------------------------------------------------------------- diversity
    async def _enforce_diversity(self, personas: list[Persona]) -> list[Persona]:
        for attempt in range(self.max_regenerations):
            pairs = description_pairs(personas, self.threshold)
            if not pairs:
                return personas
            pair = pairs[0]
            victim_idx = next(i for i, p in enumerate(personas) if p.id == pair.b)
            victim = personas[victim_idx]
            if victim.source_summary == "manual definition":
                victim_idx = next(i for i, p in enumerate(personas) if p.id == pair.a)
                victim = personas[victim_idx]
                if victim.source_summary == "manual definition":
                    log.warning("manual personas %s and %s are very similar (%.2f); keeping both",
                                pair.a, pair.b, pair.score)
                    return personas
            others = [p.traits for i, p in enumerate(personas) if i != victim_idx]
            new_traits = replacement_traits(others, seed=self.seed + attempt + 1)
            new_traits.goal = victim.traits.goal
            replacement = await self._build(new_traits, [], victim_idx, generation=victim.generation + 1)
            self.regenerated.append({"replaced": victim.id, "by": replacement.id, "similar_to": pair.a,
                                     "score": pair.score})
            log.info("persona %s too similar to %s (%.2f); regenerated as %s", victim.id, pair.a,
                     pair.score, replacement.id)
            personas[victim_idx] = replacement
        return personas

    @staticmethod
    def _assign_engines(personas: list[Persona], engines: list[str]) -> None:
        """Spread personas across engines/models, e.g. ["heuristic", "anthropic:claude-haiku-5-5"]."""
        for i, persona in enumerate(personas):
            if engines:
                engine, _, model = engines[i % len(engines)].partition(":")
                persona.engine, persona.model = engine, model or None


# ------------------------------------------------------------------- storage


def save_personas(db: Database, personas: list[Persona], study: str | None = None) -> None:
    for p in personas:
        db.insert("personas", {
            "id": p.id, "created_at": utcnow(), "study": study, "label": p.label,
            "traits_json": p.traits.model_dump(), "narrative": p.narrative,
            "source_summary": p.source_summary, "low_confidence": p.low_confidence,
            "engine": p.engine, "model": p.model, "generation": p.generation,
        }, replace=True)
        for quote in p.quotes:
            db.insert("persona_sources", {"persona_id": p.id, "source_file": p.source_summary, "snippet": quote})


def load_personas(db: Database, ids: list[str] | None = None, study: str | None = None) -> list[Persona]:
    if ids:
        rows = [db.one("SELECT * FROM personas WHERE id = ?", [i]) for i in ids]
        missing = [i for i, r in zip(ids, rows, strict=False) if r is None]
        if missing:
            raise KeyError(f"unknown persona ids: {missing}")
    elif study:
        rows = db.all("SELECT * FROM personas WHERE study = ? ORDER BY rowid", [study])
    else:
        rows = db.all("SELECT * FROM personas ORDER BY rowid")
    personas = []
    for row in rows:
        quotes = [r["snippet"] for r in db.all("SELECT snippet FROM persona_sources WHERE persona_id = ?", [row["id"]])]
        personas.append(Persona(id=row["id"], label=row["label"], traits=Traits.model_validate(row["traits_json"]),
                                narrative=row["narrative"], source_summary=row["source_summary"] or "",
                                quotes=quotes, engine=row["engine"], model=row["model"],
                                generation=row["generation"]))
    return personas
