import random

from proofloop.core.config import NoiseConfig
from proofloop.personas.models import Traits
from proofloop.web.runner.behavior import Frustration, Noise, PatienceBudget
from proofloop.web.runner.memory import SessionMemory
from proofloop.web.runner.types import Action, Signals


def test_patience_budget_scales_with_patience_and_time_pressure(profile):
    low = PatienceBudget.for_persona(Traits(patience=1), profile, time_pressure=False)
    high = PatienceBudget.for_persona(Traits(patience=5), profile, time_pressure=False)
    rushed = PatienceBudget.for_persona(Traits(patience=5), profile, time_pressure=True)
    assert low.max_steps < high.max_steps and low.max_retries < high.max_retries
    assert rushed.max_steps < high.max_steps and rushed.max_sim_seconds < high.max_sim_seconds
    assert low.wait_tolerance_s < high.wait_tolerance_s
    assert PatienceBudget.for_persona(Traits(patience=5), profile, time_pressure=False, task_cap=4).max_steps == 4


def test_frustration_rises_with_problems_and_falls_with_progress(profile):
    f = Frustration.for_persona(Traits(patience=1, mood="stressed"), profile, time_pressure=False, slow_threshold_s=3)
    f.update(Signals(error_seen=True, unexplained_error=True))
    assert f.score > 20
    before = f.score
    f.update(Signals(progress=True))
    assert f.score < before
    f.update(Signals(data_lost=True))
    f.update(Signals(slow_load=True, load_seconds=9))
    assert f.exceeded and f.top_causes()[0] in ("data_lost", "slow_load", "error_seen")
    calm = Frustration.for_persona(Traits(patience=1), profile, time_pressure=False, slow_threshold_s=3)
    calm.update(Signals(error_seen=True))
    stressed = Frustration.for_persona(Traits(patience=1, mood="stressed"), profile, time_pressure=False, slow_threshold_s=3)
    stressed.update(Signals(error_seen=True))
    assert stressed.score > calm.score


def _rates(skill, motor="none", runs=4000):
    cfg = NoiseConfig(misclick_rate_by_skill={1: 0.15, 5: 0.015}, typo_rate_by_skill={1: 0.06, 5: 0.006})
    noise = Noise(cfg, Traits(tech_skill=skill, motor=motor), random.Random(1))
    moved = sum(noise.apply(Action("click", x=500, y=300), (1280, 800))[1] for _ in range(runs))
    return moved / runs


def test_noise_is_higher_for_low_skill_and_motor_issues():
    assert _rates(1) > 3 * _rates(5)
    assert _rates(5, "tremor") > _rates(5)


def test_noise_never_touches_credentials_and_can_be_disabled():
    cfg = NoiseConfig(enabled=True, misclick_rate_by_skill={1: 0.0}, typo_rate_by_skill={1: 1.0})
    noise = Noise(cfg, Traits(tech_skill=1), random.Random(2))
    executed, changed = noise.apply(Action("fill", x=1, y=1, text="{{cred:password}}"), (100, 100))
    assert executed.text == "{{cred:password}}"
    text = "hello there this is a longer example"
    executed, changed = noise.apply(Action("fill", x=1, y=1, text=text), (100, 100))
    assert changed and executed.text != text
    off = Noise(NoiseConfig(enabled=False), Traits(tech_skill=1), random.Random(2))
    assert off.apply(Action("click", x=5, y=5), (100, 100)) == (Action("click", x=5, y=5), False)


def test_memory_degrades_on_interruption():
    mem = SessionMemory(facts=[f"fact {i}" for i in range(20)])
    mem.typed = {"email": "x"}
    forgotten = mem.degrade(0.3, random.Random(3))
    assert 8 <= len(forgotten) <= 20 and len(mem.facts) == 20 - len(forgotten)
    restored = SessionMemory.from_long_term(mem.long_term())
    assert restored.visits == 1 and restored.facts == mem.facts[-12:]
