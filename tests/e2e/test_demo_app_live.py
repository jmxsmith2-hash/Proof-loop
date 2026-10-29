"""Same quality bar as the offline test, but personas and judge run on Claude.

Skipped unless PROOFLOOP_LIVE=1 and ANTHROPIC_API_KEY are set. Costs money:
run `proofloop web run studies/demo.yaml --dry-run` with the same engines to see the estimate.
"""

import asyncio
import os

import pytest
import yaml

from proofloop.core.db import Database
from proofloop.personas.generate import PersonaGenerator, save_personas
from proofloop.web.judge.pipeline import verify_run
from proofloop.web.runner.orchestrator import Orchestrator

from .test_demo_app_offline import REPO, matches

pytestmark = [pytest.mark.browser, pytest.mark.live,
              pytest.mark.skipif(not (os.environ.get("PROOFLOOP_LIVE") == "1" and os.environ.get("ANTHROPIC_API_KEY")),
                                 reason="live model test: set PROOFLOOP_LIVE=1 and ANTHROPIC_API_KEY")]


def test_live_model_personas_find_planted_flaws(tmp_path, settings, profile, demo_study):
    settings.engine.roles = {"actor": "anthropic", "judge": "anthropic", "persona": "anthropic"}
    settings.run.concurrency = 3
    db = Database(tmp_path / "live.sqlite")
    personas = asyncio.run(PersonaGenerator(seed=7).generate(3, [REPO / "examples" / s for s in demo_study.personas.sources]))
    save_personas(db, personas, demo_study.name)
    orch = Orchestrator(settings, db, demo_study, profile)
    assert orch.estimate(personas, repeats=2).total_usd < settings.run.max_cost_usd
    run_id = asyncio.run(orch.run(personas, repeats=2))
    asyncio.run(verify_run(settings, db, demo_study, profile, run_id))
    from proofloop.web.aggregate import aggregate_run
    asyncio.run(aggregate_run(db, run_id, settings.run.promote_min))
    truth = yaml.safe_load((REPO / "src" / "proofloop" / "web" / "demo" / "planted_flaws.yaml").read_text(encoding="utf-8"))
    promoted = db.all("SELECT * FROM issue_clusters WHERE run_id = ? AND promoted = 1", [run_id])
    found = [f["id"] for f in truth["flaws"] if any(matches(c, f) for c in promoted)]
    assert len(found) >= 3, f"found only {found}"
    on_controls = [c["title"] for c in promoted if set(c["detail_json"]["tasks"]) & set(truth["controls"]["tasks"])]
    assert not on_controls
