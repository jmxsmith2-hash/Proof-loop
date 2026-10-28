"""Verification pipeline against the demo app: judge, replay, questionnaires."""

import asyncio

import pytest

from synthux.core.db import Database, new_id
from synthux.judge.pipeline import verify_run
from synthux.personas.generate import save_personas
from synthux.personas.models import Persona, Traits
from synthux.runner.orchestrator import Orchestrator

pytestmark = pytest.mark.browser


def test_verify_confirms_real_issues_and_rejects_made_up_ones(settings, profile, demo_study, tmp_path):
    settings.noise.enabled = False
    settings.run.concurrency = 4
    db = Database(tmp_path / "v.sqlite")
    p = Persona(id="per_v", label="steady", traits=Traits(tech_skill=3, patience=4), narrative="steady person")
    save_personas(db, [p], "demo")
    run_id = asyncio.run(Orchestrator(settings, db, demo_study, profile).run([p], repeats=2,
                                                                              task_ids=["shipping", "contact"]))
    # A made-up issue: the persona claims the contact button did nothing, on a step where it worked.
    contact = db.one("SELECT * FROM sessions WHERE run_id = ? AND task_id = 'contact'", [run_id])
    step = db.one("SELECT * FROM steps WHERE session_id = ? AND idx = 0", [contact["id"]])
    db.insert("issues", {"id": new_id("iss"), "run_id": run_id, "session_id": contact["id"], "persona_id": p.id,
                         "task_id": "contact", "step_idx": 0, "screen_id": "/", "category": "dead_click",
                         "region_json": {"x": 280, "y": 150, "w": 120, "h": 40}, "title": "Click had no effect",
                         "description": "Clicking 'Contact us' did nothing visible", "severity": 2,
                         "source": "behavior", "failure_class": "UX_ISSUE", "status": "pending",
                         "evidence_json": {"screenshot": step["screenshot_path"], "steps": ["click 'Contact us'"]}})
    db.execute("UPDATE steps SET signals_json = ? WHERE id = ?", ['{"no_change": true}', step["id"]])

    summary = asyncio.run(verify_run(settings, db, demo_study, profile, run_id))
    issues = db.all("SELECT * FROM issues WHERE run_id = ?", [run_id])
    slow = [i for i in issues if i["category"] == "slow_response"]
    assert slow and all(i["status"] == "accepted" and i["replay_status"] == "CONFIRMED" for i in slow)
    fake = next(i for i in issues if i["category"] == "dead_click")
    assert fake["replay_status"] == "NOT_REPRODUCED"     # the judge let it through on paper; replay caught it
    stated = [i for i in issues if i["source"] == "elicited"]
    assert stated and all(i["status"] == "rejected" for i in stated if i["screen_id"] in ("/", "/contact"))
    assert len(db.all("SELECT * FROM questionnaires")) == 4
    assert db.all("SELECT * FROM judge_reviews") and summary["replay"]
