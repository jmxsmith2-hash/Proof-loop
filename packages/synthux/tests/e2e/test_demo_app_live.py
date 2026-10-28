"""Same quality bar as the offline test, but personas and judge run on Claude.

Skipped unless SYNTHUX_LIVE=1 and ANTHROPIC_API_KEY are set. Costs money:
run `synthux run studies/demo.yaml --dry-run` with the same engines to see the estimate.
"""

import asyncio
import os
from pathlib import Path

import pytest
import yaml

from synthux.core.db import Database
from synthux.judge.pipeline import verify_run
from synthux.personas.generate import PersonaGenerator, save_personas
from synthux.runner.orchestrator import Orchestrator

from .test_demo_app_offline import REPO, matches

pytestmark = [pytest.mark.browser, pytest.mark.live,
              pytest.mark.skipif(not (os.environ.get("SYNTHUX_LIVE") == "1" and os.environ.get("ANTHROPIC_API_KEY")),
                                 reason="live model test: set SYNTHUX_LIVE=1 and ANTHROPIC_API_KEY")]


def test_live_model_personas_find_planted_flaws(tmp_path, settings, profile, demo_study):
    settings.engine.roles = {"actor": "anthropic", "judge": "anthropic", "persona": "anthropic"}
    settings.run.concurrency = 3
    db = Database(tmp_path / "live.sqlite")
    personas = asyncio.run(PersonaGenerator(seed=7).generate(3, [REPO / s for s in demo_study.personas.sources]))
    save_personas(db, personas, demo_study.name)
    orch = Orchestrator(settings, db, demo_study, profile)
    assert orch.estimate(personas, repeats=2).total_usd < settings.run.max_cost_usd
    run_id = asyncio.run(orch.run(personas, repeats=2))
    asyncio.run(verify_run(settings, db, demo_study, profile, run_id))
    from synthux.aggregate import aggregate_run
    asyncio.run(aggregate_run(db, run_id, settings.run.promote_min))
    truth = yaml.safe_load((REPO / "demo_app" / "planted_flaws.yaml").read_text())
    promoted = db.all("SELECT * FROM issue_clusters WHERE run_id = ? AND promoted = 1", [run_id])
    found = [f["id"] for f in truth["flaws"] if any(matches(c, f) for c in promoted)]
    assert len(found) >= 3, f"found only {found}"
    on_controls = [c["title"] for c in promoted if set(c["detail_json"]["tasks"]) & set(truth["controls"]["tasks"])]
    assert not on_controls
