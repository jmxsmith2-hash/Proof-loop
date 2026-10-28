"""Aggregation: cluster duplicates, promote recurring issues, rank, store clusters."""

from __future__ import annotations

from typing import Any

from synthux.aggregate.dedupe import Cluster, build_clusters, cluster_issues, confirm_borderline
from synthux.aggregate.rank import score_clusters
from synthux.core.db import Database
from synthux.core.llm import LLMGateway
from synthux.core.logging import get_logger
from synthux.personas.generate import load_personas

log = get_logger(__name__)


async def aggregate_run(db: Database, run_id: str, promote_min: int,
                        gateway: LLMGateway | None = None) -> list[Cluster]:
    """Cluster the run's accepted UX issues and store ranked clusters. Returns them best first."""
    sessions = {s["id"]: s for s in db.all("SELECT * FROM sessions WHERE run_id = ?", [run_id])}
    issues = db.all("SELECT * FROM issues WHERE run_id = ? AND status = 'accepted' AND failure_class = 'UX_ISSUE' "
                    "ORDER BY rowid", [run_id])
    personas = {p.id: p for p in load_personas(db, ids=sorted({s["persona_id"] for s in sessions.values()}))}
    groups = cluster_issues(issues)
    groups = await confirm_borderline(issues, groups, gateway)
    clusters = score_clusters(build_clusters(issues, groups), sessions, personas, promote_min)
    db.execute("DELETE FROM issue_clusters WHERE run_id = ?", [run_id])
    for c in clusters:
        cid = f"{run_id}_{c.id}"
        d = c.detail
        db.insert("issue_clusters", {
            "id": cid, "run_id": run_id, "title": c.title, "category": c.category, "screen_id": c.screen_id,
            "severity": d["severity"], "runs_seen": d["recurrence"]["best_runs"],
            "runs_total": d["recurrence"]["repeats"], "personas_json": d["personas"], "score": d["score"],
            "confidence": d["confidence"], "promoted": d["promoted"], "novelty": d["novelty"],
            "low_confidence_personas": len(d["low_confidence_personas"]), "replay_status": d["replay"],
            "member_ids_json": [m["id"] for m in c.members], "detail_json": {**d, "tasks": c.tasks},
        })
        for m in c.members:
            db.update("issues", "id", m["id"], {"cluster_id": cid})
    log.info("run %s: %d clusters (%d promoted)", run_id, len(clusters), sum(c.detail["promoted"] for c in clusters))
    return clusters


def promoted(clusters: list[Cluster]) -> list[Cluster]:
    return [c for c in clusters if c.detail["promoted"]]


def cluster_summary(c: Cluster) -> dict[str, Any]:
    return {"id": c.id, "title": c.title, "category": c.category, "screen": c.screen_id, "tasks": c.tasks, **c.detail}
