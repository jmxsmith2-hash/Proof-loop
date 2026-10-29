"""End-to-end on the demo app with the free heuristic engine (no API key).

Must find every planted flaw and must not promote anything that isn't planted.
Takes several minutes: 6 personas x 6 tasks x 3 repeats in a real browser.
"""

import asyncio
import os
from pathlib import Path

import pytest
import yaml

from proofloop.core.db import Database
from proofloop.personas.generate import PersonaGenerator, save_personas
from proofloop.web.aggregate.guardrails import contains_demand_prediction
from proofloop.web.judge.pipeline import verify_run
from proofloop.web.report.build import ReportBuilder
from proofloop.web.runner.orchestrator import Orchestrator

REPO = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.browser, pytest.mark.e2e]


def matches(cluster, flaw) -> bool:
    return (flaw["task"] in cluster["detail_json"]["tasks"] and cluster["category"] in flaw["categories"]
            and cluster["screen_id"] in flaw["screens"])


@pytest.fixture(scope="module")
def e2e_run(tmp_path_factory, demo_url):
    from proofloop.core.config import AllowedHost, load_settings, load_yaml_file
    from proofloop.web.calibration.profiles import load_profile
    from proofloop.web.study import Study

    keep = os.environ.get("PROOFLOOP_E2E_DIR")  # set to keep the run's data for inspection
    tmp = Path(keep) if keep else tmp_path_factory.mktemp("e2e")
    tmp.mkdir(parents=True, exist_ok=True)
    settings = load_settings(REPO / "src" / "proofloop" / "data")
    settings.data_dir = tmp
    settings.run.concurrency = 4
    port = int(demo_url.rsplit(":", 1)[1])
    settings.safety.allowlist.append(AllowedHost(host="127.0.0.1", ports=[port], sandbox=True))
    raw = load_yaml_file(REPO / "examples" / "studies" / "demo.yaml")
    raw["target"]["url"] = demo_url
    study = Study.model_validate(raw)
    db = Database(tmp / "e2e.sqlite")
    profile = load_profile(settings, db)
    sources = [REPO / "examples" / s for s in study.personas.sources]
    personas = asyncio.run(PersonaGenerator(seed=7).generate(study.personas.count, sources))
    save_personas(db, personas, study.name)
    run_id = asyncio.run(Orchestrator(settings, db, study, profile).run(personas))
    asyncio.run(verify_run(settings, db, study, profile, run_id))
    report = asyncio.run(ReportBuilder(settings, db).build(run_id, tmp / "report"))
    return db, run_id, report


@pytest.fixture(scope="module")
def truth():
    return yaml.safe_load((REPO / "src" / "proofloop" / "web" / "demo" / "planted_flaws.yaml").read_text(encoding="utf-8"))


def test_every_planted_flaw_is_found(e2e_run, truth):
    db, run_id, _ = e2e_run
    promoted = db.all("SELECT * FROM issue_clusters WHERE run_id = ? AND promoted = 1", [run_id])
    missing = [f["id"] for f in truth["flaws"] if not any(matches(c, f) for c in promoted)]
    assert not missing, f"planted flaws not found: {missing}"


def test_no_flaws_that_are_not_there(e2e_run, truth):
    db, run_id, _ = e2e_run
    promoted = db.all("SELECT * FROM issue_clusters WHERE run_id = ? AND promoted = 1", [run_id])
    false_positives = [(c["title"], c["screen_id"]) for c in promoted
                       if not any(matches(c, f) for f in truth["flaws"])]
    assert not false_positives, f"reported flaws that were not planted: {false_positives}"
    on_controls = [c for c in promoted if set(c["detail_json"]["tasks"]) & set(truth["controls"]["tasks"])]
    assert not on_controls


def test_report_is_honest(e2e_run):
    db, run_id, report = e2e_run
    html = report.read_text(encoding="utf-8")
    assert "SYNTHETIC DATA" in html and "confirm any major decision with real users" in html
    assert not contains_demand_prediction(html)
    assert "Top " in html and "assets/" in html
    assert "Low confidence; verify with real users" in html  # the accessibility/age personas are flagged
    for c in db.all("SELECT * FROM issue_clusters WHERE run_id = ? AND promoted = 1", [run_id]):
        assert c["replay_status"] == "CONFIRMED"
        assert c["confidence"] in ("high", "medium", "low")
