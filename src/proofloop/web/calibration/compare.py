"""Compare a synthetic run against real-user results, store the comparison, suggest tuning."""

from __future__ import annotations

from typing import Any

from proofloop.core.config import Settings
from proofloop.core.db import Database, new_id, utcnow
from proofloop.core.logging import get_logger
from proofloop.web.aggregate import aggregate_run
from proofloop.web.calibration.importer import real_study
from proofloop.web.calibration.match import match_issues
from proofloop.web.calibration.metrics import (
    agreement_score,
    completion_metrics,
    issue_metrics,
    quit_metrics,
    synthetic_quit_points,
)
from proofloop.web.calibration.profiles import load_profile
from proofloop.web.calibration.tune import suggest

log = get_logger(__name__)


async def compare(settings: Settings, db: Database, run_id: str, real_study_id: str) -> dict[str, Any]:
    run = db.one("SELECT * FROM runs WHERE id = ?", [run_id])
    if run is None:
        raise KeyError(f"no run {run_id}")
    if not db.one("SELECT id FROM issue_clusters WHERE run_id = ?", [run_id]):
        await aggregate_run(db, run_id, settings.run.promote_min)
    real = real_study(db, real_study_id)
    real_tasks = {t["task_id"] for t in real["tasks"]} | {i["task_id"] for i in real["issues"] if i["task_id"]}
    run_tasks = set(run["config_json"]["tasks"])
    shared = real_tasks & run_tasks
    if not shared:
        raise ValueError(f"run {run_id} and real study {real_study_id} have no tasks in common "
                         f"(run: {sorted(run_tasks)}, real: {sorted(real_tasks)})")
    synthetic = [c for c in db.all("SELECT * FROM issue_clusters WHERE run_id = ? AND promoted = 1", [run_id])
                 if set(c["detail_json"]["tasks"]) & shared]
    real_issues = [i for i in real["issues"] if not i["task_id"] or i["task_id"] in shared]
    matches, real_only, synthetic_only = match_issues(real_issues, synthetic)
    sessions = db.all("SELECT * FROM sessions WHERE run_id = ?", [run_id])
    issues = issue_metrics(matches, real_only, synthetic_only)
    completion = completion_metrics([t for t in real["tasks"] if t["task_id"] in shared], sessions)
    quits = quit_metrics({k: v for k, v in real["quits"].items() if k in shared}, synthetic_quit_points(db, run_id))
    profile = load_profile(settings, db, run["calibration_version"])
    suggestions = suggest(profile, completion, matches, real_only, synthetic_only)
    metrics = {"issues": issues, "completion": completion, "quits": quits,
               "agreement": agreement_score(issues, completion, quits), "shared_tasks": sorted(shared)}
    result = {
        "id": new_id("cal"), "run_id": run_id, "real_study_id": real_study_id, "profile_version": profile.version,
        "metrics": metrics,
        "both": [{"real": m.real["title"], "synthetic": m.synthetic["title"], "screen": m.synthetic["screen_id"],
                  "score": m.score} for m in matches],
        "real_only": [{"title": r["title"], "screen": r.get("screen"), "task": r.get("task_id")} for r in real_only],
        "synthetic_only": [{"title": s["title"], "screen": s["screen_id"], "category": s["category"]}
                           for s in synthetic_only],
        "suggestions": suggestions,
    }
    db.insert("calibration_runs", {
        "id": result["id"], "created_at": utcnow(), "real_study_id": real_study_id, "run_id": run_id,
        "profile_version": profile.version, "metrics_json": metrics,
        "matches_json": {"both": result["both"], "real_only": result["real_only"],
                         "synthetic_only": result["synthetic_only"]},
        "suggestions_json": suggestions})
    log.info("calibration %s: agreement %.2f (F1 %.2f, completion MAE %s)", result["id"], metrics["agreement"],
             issues["f1"], completion["mae"])
    return result


def history(db: Database) -> list[dict[str, Any]]:
    rows = db.all("SELECT c.*, s.name AS real_name FROM calibration_runs c "
                  "JOIN real_user_studies s ON s.id = c.real_study_id ORDER BY c.created_at, c.rowid")
    return [{"id": r["id"], "created_at": r["created_at"], "run_id": r["run_id"], "real_study": r["real_name"],
             "profile": r["profile_version"], "applied": r["applied_version"],
             "agreement": r["metrics_json"]["agreement"], "f1": r["metrics_json"]["issues"]["f1"],
             "precision": r["metrics_json"]["issues"]["precision"], "recall": r["metrics_json"]["issues"]["recall"],
             "completion_mae": r["metrics_json"]["completion"]["mae"],
             "quit_top_agreement": r["metrics_json"]["quits"]["top_agreement"]} for r in rows]
