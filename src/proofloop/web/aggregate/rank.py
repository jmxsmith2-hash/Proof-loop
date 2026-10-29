"""Promotion, scoring and confidence for issue clusters.

score = severity weight x frequency x personas affected x behaviour factor (x 0.4 for novelty)

Behaviour outranks opinion: clusters tied to quits, retries or lost data are
boosted; clusters made only of stated complaints are halved.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from proofloop.personas.models import Persona
from proofloop.web.aggregate.dedupe import Cluster

SEVERITY_WEIGHT = {1: 1.0, 2: 2.0, 3: 4.0, 4: 8.0}
SEVERITY_NAME = {1: "cosmetic", 2: "minor", 3: "major", 4: "blocker"}


def _recurrence(cluster: Cluster, sessions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    runs_with: dict[tuple[str, str], set[int]] = defaultdict(set)
    for m in cluster.members:
        s = sessions[m["session_id"]]
        runs_with[(m["persona_id"], m["task_id"])].add(s["repeat_index"])
    totals: dict[tuple[str, str], set[int]] = defaultdict(set)
    for s in sessions.values():
        totals[(s["persona_id"], s["task_id"])].add(s["repeat_index"])
    pairs = {f"{p}/{t}": f"{len(r)}/{len(totals[(p, t)])}" for (p, t), r in runs_with.items()}
    best = max((len(r) for r in runs_with.values()), default=0)
    return {"best_runs": best, "per_pair": pairs,
            "repeats": max((len(totals[k]) for k in runs_with), default=0)}


def score_clusters(clusters: list[Cluster], sessions: dict[str, dict[str, Any]], personas: dict[str, Persona],
                   promote_min: int) -> list[Cluster]:
    task_sessions: dict[str, set[str]] = defaultdict(set)
    for s in sessions.values():
        task_sessions[s["task_id"]].add(s["id"])
    for c in clusters:
        sessions_with = {m["session_id"] for m in c.members}
        total = set().union(*(task_sessions[t] for t in c.tasks)) if c.tasks else set()
        frequency = len(sessions_with) / max(1, len(total))
        affected = sorted({m["persona_id"] for m in c.members})
        severity = max(int(m["severity"]) for m in c.members)
        quits_here = sum(1 for sid in sessions_with if sessions[sid]["outcome"] in ("quit", "budget_exhausted"))
        behaviour = [m for m in c.members if m["source"] == "behavior"]
        factor = 1.0
        if quits_here:
            factor += 0.5 * quits_here / max(1, len(sessions_with))
        if any(m["category"] in ("data_lost_on_error", "unexplained_error") for m in behaviour):
            factor += 0.25
        if not behaviour:
            factor *= 0.5
        replays = [m.get("replay_status") for m in c.members if m.get("replay_status")]
        if any(r == "CONFIRMED" for r in replays):
            replay = "CONFIRMED"
        elif replays and all(r == "NOT_REPRODUCED" for r in replays):
            replay = "NOT_REPRODUCED"
        elif replays:
            replay = "REPLAY_FAILED"
        else:
            replay = "NOT_REPLAYED"
        novelty = all(m.get("novelty") for m in c.members)
        rec = _recurrence(c, sessions)
        recurs = rec["best_runs"] >= promote_min
        promoted = recurs and replay != "NOT_REPRODUCED" and bool(behaviour)  # stated-only never promoted
        low_conf = [p for p in affected if p in personas and personas[p].low_confidence]
        score = SEVERITY_WEIGHT[severity] * frequency * len(affected) * factor * (0.4 if novelty else 1.0)
        confidence = _confidence(replay, rec["best_runs"], promote_min, len(affected), low_conf, affected, behaviour)
        c.detail = {
            "severity": severity, "severity_name": SEVERITY_NAME[severity], "frequency": round(frequency, 3),
            "sessions_with": len(sessions_with), "sessions_total": len(total), "personas": affected,
            "personas_affected": len(affected), "behaviour_factor": round(factor, 2), "quits": quits_here,
            "replay": replay, "novelty": novelty, "recurrence": rec, "recurs": recurs, "promoted": promoted,
            "score": round(score, 3), "confidence": confidence,
            "low_confidence_personas": low_conf, "only_low_confidence": bool(affected) and len(low_conf) == len(affected),
            "stated_only": not behaviour,
            "novelty_reason": next(((m.get("judge_json") or {}).get("novelty_reason") for m in c.members
                                    if (m.get("judge_json") or {}).get("novelty_reason")), ""),
            "why_not_promoted": _why_not(recurs, replay, behaviour, affected, rec, promote_min),
        }
    return sorted(clusters, key=lambda c: (-c.detail["promoted"], -c.detail["score"]))


def _confidence(replay: str, best_runs: int, promote_min: int, n_personas: int, low_conf: list[str],
                affected: list[str], behaviour: list[dict[str, Any]]) -> str:
    if replay == "NOT_REPRODUCED" or not behaviour or (affected and len(low_conf) == len(affected)):
        return "low"
    if replay == "CONFIRMED" and best_runs >= promote_min and n_personas >= 2:
        return "high"
    return "medium"


def _why_not(recurs: bool, replay: str, behaviour: list, affected: list, rec: dict, promote_min: int) -> str:
    reasons = []
    if not recurs:
        reasons.append(f"appeared in at most {rec['best_runs']} of {rec['repeats']} runs (needs {promote_min})")
    if replay == "NOT_REPRODUCED":
        reasons.append("did not reproduce on replay (likely the persona's own simulated slip)")
    if not behaviour:
        reasons.append("stated in questionnaires only, with no measured behaviour behind it")
    return "; ".join(reasons)
