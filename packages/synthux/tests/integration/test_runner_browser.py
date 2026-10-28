"""Runner against the real demo app in Chromium. Marked `browser` (about 2-4 minutes)."""

import asyncio
import json

import pytest

from synthux.core.config import AllowedHost
from synthux.core.db import Database
from synthux.core.llm import FakeLLM, LLMGateway
from synthux.core.study import Conditions
from synthux.engines.heuristic import HeuristicActor
from synthux.engines.llm_actor import LLMActor
from synthux.personas.models import Persona, Traits
from synthux.runner.browser import BrowserPool
from synthux.runner.orchestrator import Orchestrator
from synthux.runner.session import SessionRunner
from synthux.safety.allowlist import SafetyError

pytestmark = pytest.mark.browser


def persona(**traits):
    return Persona(id="per_" + "_".join(f"{k}{v}" for k, v in traits.items())[:30] or "per_x", label="test",
                   traits=Traits(**traits), narrative="A test persona.")


def run_one(settings, profile, study, task_id, p, *, actor=None, sandbox=True, tmp_path=None, **kw):
    async def go():
        async with BrowserPool(settings) as pool:
            runner = SessionRunner(settings, profile, study, sandbox=sandbox, out_dir=tmp_path)
            return await runner.run(pool, p, study.task(task_id), actor or HeuristicActor(), seed=3, **kw)
    return asyncio.run(go())


def categories(result):
    return {i.category for i in result.issues if i.failure_class == "UX_ISSUE"}


@pytest.fixture
def quiet(settings):
    settings.noise.enabled = False  # deterministic for these checks
    return settings


def test_planted_flaws_are_found(quiet, profile, demo_study, tmp_path):
    steady = persona(tech_skill=3, patience=4)
    signup = run_one(quiet, profile, demo_study, "signup", steady, tmp_path=tmp_path)
    assert {"data_lost_on_error", "unexplained_error", "ambiguous_label"} <= categories(signup)
    plan = run_one(quiet, profile, demo_study, "choose_plan", steady, tmp_path=tmp_path)
    assert "hidden_control" in categories(plan)
    ship = run_one(quiet, profile, demo_study, "shipping", steady, tmp_path=tmp_path)
    assert {"slow_response", "no_loading_feedback"} <= categories(ship)
    assert ship.outcome == "completed"
    order = run_one(quiet, profile, demo_study, "place_order", steady, tmp_path=tmp_path)
    assert "unexplained_error" in categories(order) and order.outcome == "completed"


def test_clean_pages_produce_no_issues(quiet, profile, demo_study, tmp_path):
    p = persona(tech_skill=3, patience=3)
    for task in ("login", "contact"):
        result = run_one(quiet, profile, demo_study, task, p, tmp_path=tmp_path)
        assert result.outcome == "completed", (task, result.quit_reason)
        assert categories(result) == set(), (task, result.issues)


def test_low_vision_persona_quits_on_hidden_button(quiet, profile, demo_study, tmp_path):
    result = run_one(quiet, profile, demo_study, "choose_plan", persona(eyesight="low_vision", patience=5), tmp_path=tmp_path)
    assert result.outcome == "quit" and "continue" in result.quit_reason.lower()
    assert any(i.category == "hidden_control" and i.severity == 4 for i in result.issues)


def test_think_aloud_is_logged_every_step(quiet, profile, demo_study, tmp_path):
    result = run_one(quiet, profile, demo_study, "signup", persona(), tmp_path=tmp_path)
    assert result.steps
    for step in result.steps:
        assert step.think.sees and step.think.expects and step.think.does and step.think.feels
        assert (tmp_path / result.session_id / f"{step.idx:03d}.jpg").is_file()


def test_safety_blocks_payment_on_non_sandbox_target(quiet, profile, demo_study, tmp_path):
    result = run_one(quiet, profile, demo_study, "place_order", persona(), sandbox=False, tmp_path=tmp_path)
    assert result.outcome == "blocked"
    assert [i.failure_class for i in result.issues] == ["BLOCKED_BY_SAFETY"]


def test_navigation_off_allowlist_is_refused(quiet, demo_study):
    async def go():
        async with BrowserPool(quiet) as pool:
            s = await pool.session(device=quiet.device("desktop"), network=quiet.network("fast"),
                                   safety=quiet.safety, sandbox=True)
            try:
                with pytest.raises(SafetyError):
                    await s.goto("https://example.com/")
            finally:
                await s.close()
    asyncio.run(go())


def test_model_persona_sees_only_pixels(settings, profile, demo_study, tmp_path):
    """The prompt sent to a model persona contains no HTML/DOM; only a screenshot and text we wrote."""
    settings.engine.roles = {"actor": "fake"}
    replies = iter([
        {"sees": "a form", "expects": "typing works", "does": "type email", "feels": "ok", "confusion": 0.1,
         "action": {"kind": "fill", "x": 500, "y": 196, "text": "tester@example.test", "key": "", "dy": 0, "target": "Email"},
         "observations": [], "quit_reason": "", "limitation": ""},
        {"sees": "a form", "expects": "-", "does": "leave", "feels": "bored", "confusion": 0.0,
         "action": {"kind": "quit", "x": 0, "y": 0, "text": "", "key": "", "dy": 0, "target": ""},
         "observations": [], "quit_reason": "done testing", "limitation": ""},
    ])
    fake = FakeLLM(lambda r: next(replies))
    gateway = LLMGateway(settings, clients={"fake": fake})
    result = run_one(settings, profile, demo_study, "login", persona(), actor=LLMActor(gateway), tmp_path=tmp_path)
    assert result.outcome == "quit" and len(fake.calls) == 2
    for call in fake.calls:
        assert call.images and call.images[0][:8] == b"\x89PNG\r\n\x1a\n"
        blob = call.system + call.prompt
        for dom_marker in ("<input", "<form", "<div", "class=", "type=\"password\"", "autocomplete", "demo-pass"):
            assert dom_marker not in blob
        assert "{{cred:password}}" in blob  # credentials stay placeholders


def test_interruption_with_reload_loses_form_data(quiet, profile, demo_study, tmp_path):
    cond = Conditions(interruption_step=1, interruption_memory_keep=1.0, interruption_reload=True)
    result = run_one(quiet, profile, demo_study, "login", persona(patience=4), conditions=cond, tmp_path=tmp_path)
    assert result.outcome == "completed"
    assert any(o.kind == "interruption" for s in result.steps for o in s.observations)
    assert len(result.steps) > 3                      # the email had to be typed again
    assert "data_lost_on_error" not in categories(result)  # the simulated reload is not blamed on the product


def test_return_visit_is_faster(quiet, profile, demo_study, tmp_path):
    from synthux.runner.memory import SessionMemory

    p = persona(tech_skill=3, patience=4)
    first = run_one(quiet, profile, demo_study, "choose_plan", p, tmp_path=tmp_path)
    memory = SessionMemory.from_long_term(first.memory)
    second = run_one(quiet, profile, demo_study, "choose_plan", p, tmp_path=tmp_path, visit_index=1, memory=memory)
    assert first.outcome == second.outcome == "completed"
    assert len(second.steps) < len(first.steps)
    assert second.final_frustration < first.final_frustration


def test_mobile_and_slow_network(quiet, profile, demo_study, tmp_path):
    cond = Conditions(device="mobile", network="3g")
    result = run_one(quiet, profile, demo_study, "contact", persona(device="mobile"), conditions=cond, tmp_path=tmp_path)
    assert result.outcome == "completed" and result.conditions["network"] == "3g"
    from PIL import Image
    assert Image.open(result.steps[0].screenshot_path).size[0] == 390


def test_orchestrator_stores_everything(settings, profile, demo_study, tmp_path):
    settings.run.concurrency = 3
    db = Database(tmp_path / "o.sqlite")
    people = [persona(tech_skill=2, patience=2), persona(tech_skill=5, patience=4)]
    orch = Orchestrator(settings, db, demo_study, profile)
    assert orch.estimate(people, repeats=2).total_usd == 0.0
    run_id = asyncio.run(orch.run(people, repeats=2, task_ids=["contact", "place_order"]))
    sessions = db.all("SELECT * FROM sessions WHERE run_id = ?", [run_id])
    assert len(sessions) == 8
    assert db.one("SELECT status FROM runs WHERE id = ?", [run_id])["status"] == "done"
    assert db.all("SELECT * FROM steps")
    issues = db.all("SELECT * FROM issues WHERE run_id = ?", [run_id])
    assert any(i["category"] == "unexplained_error" for i in issues)
    assert all(i["evidence_json"]["screenshot"] for i in issues)
