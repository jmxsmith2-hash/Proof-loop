"""Automatic calibration search: try simulator settings and keep what matches real users best.

Real-user results (`synthux calibrate import`) give completion rates and where people
quit. The search re-runs the study's shared tasks with scaled settings, one knob at
a time (coordinate search), and scores each trial on how close completion and quit
points come to the real ones. Every trial uses the same random seeds (the same
misclicks and typos), so differences between trials come from the settings alone. Only the free engines are searched by default: a
paid engine would be charged for every trial.

Knobs, each a scale on the current profile:

* patience   - steps and time a persona will spend (patience.base_steps, seconds_by_patience)
* frustration - how much frustration it takes to quit (frustration.threshold_by_patience)
* perception - how much contrast a persona needs to notice text (perception.glance_contrast)
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Callable

from synthux.calibration.importer import real_study
from synthux.calibration.metrics import completion_metrics, quit_metrics, synthetic_quit_points
from synthux.calibration.profiles import CalibrationProfile, load_profile, store_profile
from synthux.core.config import Settings
from synthux.core.db import Database
from synthux.core.logging import get_logger
from synthux.core.study import Study
from synthux.personas.models import Persona

log = get_logger(__name__)

KNOBS: dict[str, list[float]] = {
    "frustration": [0.75, 0.9, 1.15, 1.35],
    "patience": [0.7, 0.85, 1.2, 1.45],
    "perception": [0.8, 1.25],
}
MIN_GAIN = 0.01   # a change must beat the current best by this much to be kept


@dataclass
class Trial:
    scales: dict[str, float]
    version: str
    run_id: str
    score: float
    completion_mae: float | None
    quit_agreement: float | None


@dataclass
class SearchResult:
    base_version: str
    best: Trial
    trials: list[Trial] = field(default_factory=list)
    profile: CalibrationProfile | None = None
    shared_tasks: list[str] = field(default_factory=list)

    @property
    def improved(self) -> bool:
        return self.best.scales != {k: 1.0 for k in self.best.scales}


def scaled_profile(base: CalibrationProfile, scales: dict[str, float], version: str) -> CalibrationProfile:
    data = copy.deepcopy(base.model_dump())
    p, f, g = scales.get("patience", 1.0), scales.get("frustration", 1.0), scales.get("perception", 1.0)
    data["patience"]["base_steps"] = max(data["patience"]["min_steps"], round(data["patience"]["base_steps"] * p))
    data["patience"]["seconds_by_patience"] = {k: round(v * p, 1) for k, v in data["patience"]["seconds_by_patience"].items()}
    data["frustration"]["threshold_by_patience"] = {k: round(v * f, 1)
                                                    for k, v in data["frustration"]["threshold_by_patience"].items()}
    data["perception"]["glance_contrast"] = {k: round(max(1.05, v * g), 2)
                                             for k, v in data["perception"]["glance_contrast"].items()}
    data["version"] = version
    return CalibrationProfile.model_validate(data)


def score_run(db: Database, run_id: str, real: dict[str, Any], tasks: list[str]) -> tuple[float, float | None, float | None]:
    """0..1, higher is better: completion closeness and quit-point agreement (equal weight)."""
    sessions = db.all("SELECT * FROM sessions WHERE run_id = ?", [run_id])
    completion = completion_metrics([t for t in real["tasks"] if t["task_id"] in tasks], sessions)
    quits = quit_metrics({k: v for k, v in real["quits"].items() if k in tasks}, synthetic_quit_points(db, run_id))
    parts = []
    if completion["mae"] is not None:
        parts.append(max(0.0, 1 - 2 * completion["mae"]))
    if quits["top_agreement"] is not None:
        parts.append(quits["top_agreement"])
    return (round(mean(parts), 3) if parts else 0.0), completion["mae"], quits["top_agreement"]


async def search(settings: Settings, db: Database, study: Study, real_study_id: str, personas: list[Persona], *,
                 repeats: int = 1, rounds: int = 2, knobs: dict[str, list[float]] | None = None,
                 profile_name: str | None = None,
                 on_trial: Callable[[Trial], None] | None = None) -> SearchResult:
    from synthux.runner.orchestrator import Orchestrator

    knobs = knobs or KNOBS
    base = load_profile(settings, db, profile_name)
    real = real_study(db, real_study_id)
    real_tasks = {t["task_id"] for t in real["tasks"]}
    shared = sorted(real_tasks & {t.id for t in study.tasks})
    if not shared:
        raise ValueError(f"the study and real study {real_study_id} have no tasks in common")
    store_profile(db, base, parent=None, note="base of a calibration search")
    root = base.version.split("-search")[0]
    done: dict[tuple, Trial] = {}
    trials: list[Trial] = []
    counter = len(db.all("SELECT version FROM calibration_profiles WHERE version LIKE ?", [f"{root}-search%"]))

    async def evaluate(scales: dict[str, float]) -> Trial:
        nonlocal counter
        key = tuple(sorted(scales.items()))
        if key in done:
            return done[key]
        counter += 1
        version = f"{root}-search{counter}"
        profile = scaled_profile(base, scales, version)
        store_profile(db, profile, parent=base.version, note="calibration search trial " +
                      ", ".join(f"{k} x{v:g}" for k, v in scales.items() if v != 1.0))
        run_id = await Orchestrator(settings, db, study, profile).run(personas, repeats=repeats, task_ids=shared,
                                                                      seed_key=seed_key)
        score, mae, quits = score_run(db, run_id, real, shared)
        trial = Trial(dict(scales), version, run_id, score, mae, quits)
        done[key] = trial
        trials.append(trial)
        log.info("calibration search %s: %s -> score %.3f", version, scales, score)
        if on_trial:
            on_trial(trial)
        return trial

    seed_key = f"calibration-search|{real_study_id}|{study.name}"  # same random slips in every trial
    current = {k: 1.0 for k in knobs}
    best = await evaluate(current)
    for _ in range(rounds):
        improved = False
        for knob, values in knobs.items():
            for value in values:
                trial = await evaluate({**current, knob: value})
                if trial.score >= best.score + MIN_GAIN:
                    best, improved = trial, True
            current = dict(best.scales)
        if not improved:
            break
    return SearchResult(base_version=base.version, best=best, trials=trials,
                        profile=load_profile(settings, db, best.version), shared_tasks=shared)
