import asyncio
from pathlib import Path

from proofloop.core.llm import FakeLLM, LLMGateway
from proofloop.personas.design import design_traits, trait_distance
from proofloop.personas.diversity import description_pairs, feedback_pairs
from proofloop.personas.generate import PersonaGenerator, load_personas, save_personas
from proofloop.personas.ingest import load_sources
from proofloop.personas.models import Persona, Traits

EX = Path(__file__).resolve().parents[2] / "examples"


def test_design_is_spread_and_covers_extremes():
    chosen = [t for t, _ in design_traits(8, seed=3)]
    skills = {t.tech_skill for t in chosen}
    patience = {t.patience for t in chosen}
    assert skills & {1, 2} and skills & {4, 5}
    assert patience & {1, 2} and patience & {4, 5}
    assert any(t.device == "mobile" for t in chosen)
    assert any(t.eyesight != "normal" or t.motor != "none" for t in chosen)
    min_gap = min(trait_distance(a, b) for i, a in enumerate(chosen) for b in chosen[i + 1:])
    assert min_gap > 0.15


def test_design_is_deterministic():
    a = [t.model_dump() for t, _ in design_traits(5, seed=11)]
    b = [t.model_dump() for t, _ in design_traits(5, seed=11)]
    assert a == b


def test_data_grounded_anchors():
    records, _ = load_sources([EX / "survey.csv", EX / "reviews.txt", EX / "interview_notes.md"])
    chosen = design_traits(6, records, seed=5)
    grounded = [recs for _, recs in chosen if recs]
    assert len(grounded) >= 2


def test_generator_end_to_end(db):
    gen = PersonaGenerator(seed=4)
    personas = asyncio.run(gen.generate(6, [EX / "survey.csv", EX / "reviews.txt"]))
    assert len(personas) == 6
    assert not description_pairs(personas)
    for p in personas:
        assert "@" not in p.narrative and "Maria" not in p.narrative
    flagged = [p for p in personas if p.low_confidence]
    assert flagged and all("verify with real users" in p.confidence_note for p in flagged)
    save_personas(db, personas, study="demo")
    loaded = load_personas(db, study="demo")
    assert [p.id for p in loaded] == [p.id for p in personas]
    assert loaded[0].traits == personas[0].traits


def test_near_duplicate_gets_regenerated():
    gen = PersonaGenerator(seed=1)
    twin = {"tech_skill": 2, "patience": 2, "device": "mobile", "goal": "buy shoes"}
    personas = asyncio.run(gen.generate(3, manual=[twin]))
    # make a near-copy of the manual persona and run the check again
    copy = Persona(id="per_copy", label=personas[0].label, traits=personas[0].traits.model_copy(),
                   narrative=personas[0].narrative)
    fixed = asyncio.run(gen._enforce_diversity([personas[0], copy, *personas[1:]]))
    assert not description_pairs(fixed)
    assert gen.regenerated and gen.regenerated[-1]["replaced"] == "per_copy"


def test_manual_personas_and_engine_spread():
    gen = PersonaGenerator(seed=2)
    personas = asyncio.run(gen.generate(
        4, manual=[{"tech_skill": 5, "patience": 1, "label": "Expert in a rush"}],
        engines=["heuristic", "anthropic:claude-haiku-5-5"]))
    assert personas[0].label == "Expert in a rush"
    assert [p.engine for p in personas] == ["heuristic", "anthropic", "heuristic", "anthropic"]
    assert personas[1].model == "claude-haiku-5-5"


def test_llm_narratives_are_anonymized(settings, db):
    settings.engine.roles = {"persona": "fake"}
    fake = FakeLLM(lambda r: {"label": "Busy parent", "narrative": "Jessica (jess@example.com) shops on her phone."})
    gen = PersonaGenerator(LLMGateway(settings, db, clients={"fake": fake}), seed=9, threshold=0.99)
    personas = asyncio.run(gen.generate(2, [EX / "survey.csv"]))
    assert fake.calls and all("@" not in c.prompt for c in fake.calls)  # only anonymized data sent
    assert "jess@example.com" not in personas[0].narrative and "Jessica" not in personas[0].narrative


def test_feedback_similarity_flags_copies():
    pairs = feedback_pairs({
        "a": "The continue button is hard to see on the plan page",
        "b": "The continue button is hard to see on the plan page",
        "c": "Shipping page loads slowly with no spinner",
    })
    assert [(p.a, p.b) for p in pairs] == [("a", "b")]


def test_sensitive_traits_flag_low_confidence():
    assert Traits(age_range="65+").sensitive_reasons() == ["age: 65+"]
    assert Traits(language="second_language").sensitive_reasons()
    assert not Traits().sensitive_reasons()
