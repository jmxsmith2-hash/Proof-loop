"""Runs every persona x task x repeat (x visit) in parallel and stores the results."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict
from typing import Any, Callable

from synthux.calibration.profiles import CalibrationProfile
from synthux.core.config import Settings
from synthux.core.cost import CostEstimate, estimate_run
from synthux.core.db import Database, new_id, utcnow
from synthux.core.llm import LLMGateway
from synthux.core.logging import get_logger
from synthux.core.study import Study, is_sandbox
from synthux.engines import actor_for
from synthux.personas.models import Persona
from synthux.runner.browser import BrowserPool
from synthux.runner.memory import SessionMemory
from synthux.runner.session import SessionRunner, step_to_row
from synthux.runner.types import SessionResult

log = get_logger(__name__)


def session_seed(run_id: str, persona_id: str, task_id: str, repeat: int) -> int:
    digest = hashlib.sha256(f"{run_id}|{persona_id}|{task_id}|{repeat}".encode()).hexdigest()
    return int(digest[:8], 16)


class Orchestrator:
    def __init__(self, settings: Settings, db: Database, study: Study, profile: CalibrationProfile,
                 gateway: LLMGateway | None = None):
        self.settings = settings
        self.db = db
        self.study = study
        self.profile = profile
        self.gateway = gateway or LLMGateway(settings, db)
        self.sandbox = is_sandbox(study, settings)

    # ---------------------------------------------------------------- plan
    def repeats(self, override: int | None = None) -> int:
        return override or self.study.repeats or self.settings.run.repeats

    def session_count(self, personas: list[Persona], repeats: int | None = None) -> int:
        return len(personas) * len(self.study.tasks) * self.repeats(repeats) * self.study.visits

    def estimate(self, personas: list[Persona], repeats: int | None = None) -> CostEstimate:
        device = self.settings.device(self.study.conditions.device or "desktop")
        avg_steps = self.profile.patience.base_steps * 0.7
        return estimate_run(self.settings, sessions=self.session_count(personas, repeats), avg_steps=avg_steps,
                            viewport=(device.width, device.height), persona_count=0)

    # ----------------------------------------------------------------- run
    async def run(self, personas: list[Persona], *, repeats: int | None = None, run_id: str | None = None,
                  task_ids: list[str] | None = None,
                  on_session: Callable[[SessionResult], None] | None = None) -> str:
        run_id = run_id or new_id("run")
        n_repeats = self.repeats(repeats)
        tasks = [t for t in self.study.tasks if not task_ids or t.id in task_ids]
        out_dir = self.settings.data_dir / "runs" / run_id
        estimate = self.estimate(personas, repeats)
        self.db.insert("runs", {
            "id": run_id, "created_at": utcnow(), "study": self.study.name, "target_url": self.study.target.url,
            "status": "running", "calibration_version": self.profile.version,
            "cost_estimate_usd": estimate.total_usd,
            "config_json": {"repeats": n_repeats, "visits": self.study.visits, "tasks": [t.id for t in tasks],
                            "personas": [p.id for p in personas], "engines": self.settings.engine.model_dump(),
                            "conditions": self.study.conditions.model_dump(), "sandbox": self.sandbox,
                            "profile_fingerprint": self.profile.fingerprint(),
                            "framing": self.study.target.framing},
        })
        self.gateway.run_id = run_id
        runner = SessionRunner(self.settings, self.profile, self.study, sandbox=self.sandbox, out_dir=out_dir)
        semaphore = asyncio.Semaphore(self.settings.run.concurrency)
        log.info("run %s: %d personas x %d tasks x %d repeats x %d visits (concurrency %d)", run_id, len(personas),
                 len(tasks), n_repeats, self.study.visits, self.settings.run.concurrency)

        async with BrowserPool(self.settings) as pool:
            async def chain(persona: Persona, task, repeat: int) -> None:
                memory = SessionMemory()
                for visit in range(self.study.visits):
                    async with semaphore:
                        result = await runner.run(
                            pool, persona, task, actor_for(persona, self.gateway),
                            seed=session_seed(run_id, persona.id, task.id, repeat) + visit,
                            repeat_index=repeat, visit_index=visit, memory=memory)
                    self._store(run_id, result)
                    if on_session:
                        on_session(result)
                    memory = SessionMemory.from_long_term(result.memory)

            jobs = [chain(p, t, r) for p in personas for t in tasks for r in range(n_repeats)]
            outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        for failure in failures:
            log.error("session chain failed: %r", failure)
        status = "done" if not failures else ("failed" if len(failures) == len(jobs) else "done_with_errors")
        self.db.update("runs", "id", run_id, {"status": status, "finished_at": utcnow(),
                                              "cost_actual_usd": round(self.gateway.spent_usd, 4)})
        if failures and len(failures) == len(jobs):
            raise failures[0]
        return run_id

    # --------------------------------------------------------------- store
    def _store(self, run_id: str, result: SessionResult) -> None:
        self.db.insert("sessions", {
            "id": result.session_id, "run_id": run_id, "persona_id": result.persona_id, "task_id": result.task_id,
            "repeat_index": result.repeat_index, "visit_index": result.visit_index, "seed": result.seed,
            "conditions_json": result.conditions, "outcome": result.outcome, "quit_reason": result.quit_reason,
            "steps_count": len(result.steps), "sim_seconds": result.sim_seconds, "wall_seconds": result.wall_seconds,
            "final_frustration": result.final_frustration,
            "memory_json": {**result.memory, "frustration_timeline": result.frustration_timeline},
            "started_at": utcnow(), "ended_at": utcnow(),
        })
        for step in result.steps:
            self.db.insert("steps", step_to_row(result.session_id, step))
        for issue in result.issues:
            self.db.insert("issues", issue_row(run_id, result, issue))


def issue_row(run_id: str, result: SessionResult, issue: Any) -> dict[str, Any]:
    return {
        "id": new_id("iss"), "run_id": run_id, "session_id": result.session_id, "persona_id": result.persona_id,
        "task_id": result.task_id, "step_idx": issue.step_idx, "screen_id": issue.screen_id,
        "region_json": issue.region, "category": issue.category, "title": issue.title,
        "description": issue.description, "severity": issue.severity, "source": issue.source,
        "failure_class": issue.failure_class, "status": "pending", "evidence_json": issue.evidence,
    }


def session_summary(result: SessionResult) -> dict[str, Any]:
    return {k: v for k, v in asdict(result).items() if k not in ("steps", "issues")}
