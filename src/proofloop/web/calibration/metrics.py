"""Agreement metrics between synthetic and real results on the same tasks."""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Any

from proofloop.web.calibration.match import Match


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or len(set(a)) < 2 or len(set(b)) < 2:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = mean(ra), mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=False))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return round(num / den, 3) if den else None


def issue_metrics(matches: list[Match], real_only: list, synthetic_only: list) -> dict[str, Any]:
    tp = len(matches)
    precision = tp / (tp + len(synthetic_only)) if tp + len(synthetic_only) else None
    recall = tp / (tp + len(real_only)) if tp + len(real_only) else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else 0.0
    sev = spearman([float(m.real.get("severity") or 2) for m in matches],
                   [float(m.synthetic["severity"]) for m in matches])
    return {"matched": tp, "real_only": len(real_only), "synthetic_only": len(synthetic_only),
            "precision": None if precision is None else round(precision, 3),
            "recall": None if recall is None else round(recall, 3), "f1": round(f1, 3),
            "severity_rank_correlation": sev}


def completion_metrics(real_tasks: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    per_task = {}
    for t in real_tasks:
        ss = [s for s in sessions if s["task_id"] == t["task_id"] and s["visit_index"] == 0]
        if not ss:
            continue
        synthetic = sum(1 for s in ss if s["outcome"] == "completed") / len(ss)
        real = t["completed"] / t["participants"] if t["participants"] else 0.0
        per_task[t["task_id"]] = {"real": round(real, 3), "synthetic": round(synthetic, 3),
                                  "delta": round(synthetic - real, 3)}
    deltas = [v["delta"] for v in per_task.values()]
    return {"per_task": per_task, "mae": round(mean(abs(d) for d in deltas), 3) if deltas else None,
            "bias": round(mean(deltas), 3) if deltas else None}


def quit_metrics(real_quits: dict[str, list[dict[str, Any]]], synthetic_quits: dict[str, Counter]) -> dict[str, Any]:
    per_task, hits, total = {}, 0, 0
    for task, quits in real_quits.items():
        real_counter = Counter({q["screen"]: int(q.get("count", 1)) for q in quits})
        syn = synthetic_quits.get(task, Counter())
        if not real_counter and not syn:
            continue
        real_top = real_counter.most_common(1)[0][0] if real_counter else None
        syn_top = syn.most_common(1)[0][0] if syn else None
        union = set(real_counter) | set(syn)
        jaccard = len(set(real_counter) & set(syn)) / len(union) if union else 1.0
        per_task[task] = {"real_top": real_top, "synthetic_top": syn_top, "same_top": real_top == syn_top,
                          "jaccard": round(jaccard, 3), "real": dict(real_counter), "synthetic": dict(syn)}
        total += 1
        hits += int(real_top == syn_top)
    return {"per_task": per_task, "top_agreement": round(hits / total, 3) if total else None}


def agreement_score(issues: dict[str, Any], completion: dict[str, Any], quits: dict[str, Any]) -> float:
    """A single 0..1 number to track over time (higher is better)."""
    parts = [issues["f1"] or 0.0]
    if completion["mae"] is not None:
        parts.append(max(0.0, 1 - completion["mae"] * 2))
    if quits["top_agreement"] is not None:
        parts.append(quits["top_agreement"])
    return round(mean(parts), 3)


def synthetic_quit_points(db, run_id: str) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for s in db.all("SELECT * FROM sessions WHERE run_id = ? AND outcome IN ('quit', 'budget_exhausted') "
                    "AND visit_index = 0", [run_id]):
        last = db.one("SELECT screen_id FROM steps WHERE session_id = ? ORDER BY idx DESC LIMIT 1", [s["id"]])
        if last:
            out[s["task_id"]][last["screen_id"]] += 1
    return out
