"""Quick website test without a study file: shared by `proofloop web test` and the app window."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from proofloop.core.config import Settings
from proofloop.core.db import Database
from proofloop.web.safety.allow_edit import host_entry
from proofloop.web.safety.allowlist import check_url
from proofloop.web.study import Study


def build_study(settings: Settings, url: str, tasks: list[str], successes: list[str], *, find: bool = False,
                personas: int = 5, repeats: int = 2, device: str | None = None) -> tuple[Study, dict[str, Any]]:
    """A one-off study for `url`. Raises ConfigError (bad address), ValueError, or SafetyError (not allowlisted)."""
    if len(successes) != len(tasks):
        raise ValueError(f"give one success text per task ({len(tasks)} tasks, {len(successes)} success texts)")
    if not tasks:
        raise ValueError("give at least one task")
    host, _ = host_entry(url)
    parts = urlsplit(url)
    start = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    raw = {"name": f"quick-{host}", "target": {"url": f"{parts.scheme}://{parts.netloc}"},
           "tasks": [{"id": f"task{i + 1}", "goal": goal, "start": start, "success_text": [done],
                      "type": "find" if find else "do"} for i, (goal, done) in enumerate(zip(tasks, successes, strict=False))],
           "personas": {"count": personas}, "repeats": repeats,
           "conditions": {"device": device} if device else {}}
    study = Study.model_validate(raw)
    for t in study.tasks:
        check_url(study.task_url(t), settings.safety.allowlist)
    return study, raw


def save_study(settings: Settings, study: Study, raw: dict[str, Any]) -> Path:
    folder = settings.data_dir / "studies"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{study.name}.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


async def run_quick_test(settings: Settings, db: Database, study: Study, *, personas: int, repeats: int,
                         approve: Callable[[Any], bool] | None = None,
                         on_session: Callable[[Any], None] | None = None,
                         on_stage: Callable[[str], None] | None = None) -> tuple[str | None, Path | None, Any]:
    """Generate personas, run, verify and build the report. Returns (run id, report, estimate).

    `approve(estimate)` is asked before a paid run; returning False stops before anything is spent.
    """
    from proofloop.personas.generate import PersonaGenerator, save_personas
    from proofloop.web.calibration.profiles import load_profile
    from proofloop.web.judge.pipeline import verify_run
    from proofloop.web.report.build import ReportBuilder
    from proofloop.web.runner.orchestrator import Orchestrator

    stage = on_stage or (lambda _msg: None)
    profile = load_profile(settings, db)
    stage("creating personas")
    people = await PersonaGenerator().generate(personas, [])
    save_personas(db, people, study=study.name)
    orch = Orchestrator(settings, db, study, profile)
    estimate = orch.estimate(people, repeats)
    if estimate.total_usd > 0:
        if settings.run.max_cost_usd and estimate.total_usd > settings.run.max_cost_usd:
            raise ValueError(f"estimated cost ${estimate.total_usd:.2f} is over run.max_cost_usd "
                             f"(${settings.run.max_cost_usd:.2f})")
        if approve is not None and not approve(estimate):
            return None, None, estimate
    stage("personas are using the site")
    run_id = await orch.run(people, on_session=on_session)
    stage("checking the findings")
    await verify_run(settings, db, study, profile, run_id)
    stage("writing the report")
    report = await ReportBuilder(settings, db).build(run_id)
    return run_id, Path(report), estimate
