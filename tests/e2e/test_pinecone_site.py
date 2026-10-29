"""Second demo site: pop-ups, deep navigation, a dead menu link; clean cookie banner and sign-up form.

Free heuristic engine, real browser. Must find all three planted flaws and nothing on the clean parts.
"""

import asyncio
import json
import os
import socket
from pathlib import Path

import pytest
import yaml

from proofloop.core.db import Database
from proofloop.personas.generate import PersonaGenerator, save_personas
from proofloop.web.judge.pipeline import verify_run
from proofloop.web.runner.orchestrator import Orchestrator

from .test_demo_app_offline import REPO

pytestmark = [pytest.mark.browser, pytest.mark.e2e]


def matches(cluster, flaw) -> bool:
    return (bool(set(flaw["tasks"]) & set(cluster["detail_json"]["tasks"])) and cluster["category"] in flaw["categories"]
            and cluster["screen_id"] in flaw["screens"])


@pytest.fixture(scope="module")
def pinecone_run(tmp_path_factory):
    from proofloop.core.config import AllowedHost, load_settings, load_yaml_file
    from proofloop.web.calibration.profiles import load_profile
    from proofloop.web.demo.pinecone import PineconeServer
    from proofloop.web.study import Study

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    keep = os.environ.get("PROOFLOOP_E2E_DIR")  # set to keep the run's data for inspection
    tmp = Path(keep) if keep else tmp_path_factory.mktemp("pinecone")
    tmp.mkdir(parents=True, exist_ok=True)
    settings = load_settings(REPO / "src" / "proofloop" / "data")
    settings.data_dir = tmp
    settings.run.concurrency = 4
    settings.safety.allowlist.append(AllowedHost(host="127.0.0.1", ports=[port], sandbox=True))
    raw = load_yaml_file(REPO / "examples" / "studies" / "pinecone.yaml")
    raw["target"]["url"] = f"http://127.0.0.1:{port}"
    raw["repeats"] = 2
    study = Study.model_validate(raw)
    db = Database(tmp / "pinecone.sqlite")
    profile = load_profile(settings, db)
    personas = asyncio.run(PersonaGenerator(seed=11).generate(study.personas.count,
                                                              [REPO / s for s in study.personas.sources]))
    save_personas(db, personas, study.name)
    with PineconeServer(port):
        run_id = asyncio.run(Orchestrator(settings, db, study, profile).run(personas))
        asyncio.run(verify_run(settings, db, study, profile, run_id))
    from proofloop.web.aggregate import aggregate_run

    asyncio.run(aggregate_run(db, run_id, settings.run.promote_min))
    return db, run_id


@pytest.fixture(scope="module")
def truth():
    return yaml.safe_load((REPO / "src" / "proofloop" / "web" / "demo" / "pinecone_flaws.yaml").read_text(encoding="utf-8"))


def promoted(db, run_id):
    return db.all("SELECT * FROM issue_clusters WHERE run_id = ? AND promoted = 1", [run_id])


def test_planted_flaws_found(pinecone_run, truth):
    db, run_id = pinecone_run
    clusters = promoted(db, run_id)
    missing = [f["id"] for f in truth["flaws"] if not any(matches(c, f) for c in clusters)]
    assert not missing, f"not found: {missing}; promoted: {[(c['category'], c['screen_id']) for c in clusters]}"


def test_clean_parts_stay_clean(pinecone_run, truth):
    db, run_id = pinecone_run
    clusters = promoted(db, run_id)
    on_controls = [c["title"] for c in clusters if set(c["detail_json"]["tasks"]) & set(truth["controls"]["tasks"])]
    assert not on_controls, on_controls
    text = json.dumps([dict(c) for c in clusters], default=str).lower()
    assert not [w for w in truth["controls"]["forbidden_text"] if w in text]


def test_most_sessions_finish(pinecone_run):
    db, run_id = pinecone_run
    done = db.one("SELECT AVG(outcome = 'completed') AS r FROM sessions WHERE run_id = ?", [run_id])["r"]
    assert done >= 0.6, f"completion {done:.0%}: personas are getting stuck on ordinary site patterns"
