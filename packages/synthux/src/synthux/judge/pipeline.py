"""Verification pipeline run after the browser sessions:

1. questionnaires (behavioural questions, located complaints)
2. judge review of every issue, with send-back rounds for specifics
3. automatic replay of accepted issues
4. novelty labelling (unfamiliar vs actually hard)

Promotion by recurrence happens at aggregation time (see aggregate.dedupe).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from synthux.calibration.profiles import CalibrationProfile
from synthux.core.config import Settings
from synthux.core.db import Database, new_id, utcnow
from synthux.core.llm import LLMGateway
from synthux.core.logging import get_logger
from synthux.core.study import Study, is_sandbox
from synthux.elicitation.questions import HeuristicElicitor, ModelElicitor, load_scales
from synthux.judge.novelty import classify
from synthux.judge.repeat import group_key
from synthux.judge.replay import Replayer
from synthux.judge.review import ModelJudge, RuleJudge, revise_issue
from synthux.personas.generate import load_personas
from synthux.runner.browser import BrowserPool

log = get_logger(__name__)


def _load(db: Database, run_id: str) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    sessions = db.all("SELECT * FROM sessions WHERE run_id = ? ORDER BY rowid", [run_id])
    steps: dict[str, list[dict]] = defaultdict(list)
    for row in db.all("SELECT s.* FROM steps s JOIN sessions x ON x.id = s.session_id WHERE x.run_id = ? "
                      "ORDER BY s.session_id, s.idx", [run_id]):
        steps[row["session_id"]].append(row)
    issues = db.all("SELECT * FROM issues WHERE run_id = ? ORDER BY rowid", [run_id])
    return sessions, steps, issues


MAX_PARALLEL_CALLS = 16   # model calls in flight at once during verification


def _parallel(settings: Settings) -> int:
    """In batch mode everything goes in at once, so it lands in one batch instead of many in a row."""
    return 100_000 if settings.engine.batch else MAX_PARALLEL_CALLS


async def elicit(settings: Settings, db: Database, study: Study, run_id: str, gateway: LLMGateway) -> int:
    sessions, steps, issues = _load(db, run_id)
    personas = {p.id: p for p in load_personas(db, ids=sorted({s["persona_id"] for s in sessions}))}
    scales = load_scales(settings.config_dir)
    heuristic = HeuristicElicitor(scales)
    model = ModelElicitor(gateway, scales) if gateway.is_model_backed("persona") else None
    added = 0
    todo = [s for s in sessions if steps.get(s["id"]) and
            not db.one("SELECT id FROM questionnaires WHERE session_id = ?", [s["id"]])]
    limit = asyncio.Semaphore(_parallel(settings))

    async def ask(session: dict[str, Any]) -> Any:
        rows = steps[session["id"]]
        persona = personas[session["persona_id"]]
        task = study.task(session["task_id"])
        if model is not None and (persona.engine or gateway.engine_for("persona")) != "heuristic":
            async with limit:
                return await model.answer(persona, session, rows, task.price_points, study.target.framing,
                                          study.target.competitor_label)
        own = [i for i in issues if i["session_id"] == session["id"]]
        return heuristic.answer(persona, session, rows, task.price_points, study.target.framing, own)

    answers = await asyncio.gather(*(ask(s) for s in todo))   # concurrently; stored in session order
    for session, q in zip(todo, answers):
        rows = steps[session["id"]]
        persona = personas[session["persona_id"]]
        db.insert("questionnaires", {"session_id": session["id"], "persona_id": persona.id, "answers_json": q.to_json()})
        for c in q.complaints:
            if c.category != "elicited":
                continue  # a restatement of a behavioural issue already on record
            db.insert("issues", {
                "id": new_id("iss"), "run_id": run_id, "session_id": session["id"], "persona_id": persona.id,
                "task_id": session["task_id"], "step_idx": c.step_idx, "screen_id": c.screen_id,
                "region_json": c.region, "category": "elicited", "title": f"Complaint about '{c.element[:40]}'",
                "description": f"{c.what} ({c.why})", "severity": c.severity, "source": "elicited",
                "failure_class": "UX_ISSUE", "status": "pending",
                "evidence_json": {"screenshot": c.screenshot, "steps": [r["intended_json"].get("kind") for r in rows
                                                                        if r["idx"] <= c.step_idx],
                                  "think_aloud": next((r["think_json"] for r in rows if r["idx"] == c.step_idx), {}),
                                  "stated_only": True},
            })
            added += 1
    return added


async def judge_all(settings: Settings, db: Database, profile: CalibrationProfile, run_id: str,
                    gateway: LLMGateway) -> dict[str, int]:
    _, steps, issues = _load(db, run_id)
    slow = settings.browser.slow_load_seconds
    judge = ModelJudge(gateway, slow) if gateway.is_model_backed("judge") else RuleJudge(slow)
    counts: dict[str, int] = defaultdict(int)
    limit = asyncio.Semaphore(_parallel(settings))

    async def review(issue: dict[str, Any]) -> tuple[dict[str, Any], Any, list[tuple[int, Any]]]:
        current = dict(issue)
        verdict = None
        rounds: list[tuple[int, Any]] = []
        for round_no in range(1, profile.judge.max_rounds + 2):
            async with limit:
                verdict = await judge.review(current, steps.get(issue["session_id"], []))
            rounds.append((round_no, verdict))
            if verdict.verdict != "revise":
                break
            if round_no > profile.judge.max_rounds:
                verdict.verdict = "reject"
                verdict.reasons.append(f"still vague after {profile.judge.max_rounds} send-backs")
                break
            revised = revise_issue(current, verdict.feedback)  # the persona is sent back for specifics
            if revised is None:
                verdict.verdict = "reject"
                verdict.reasons.append("the persona could not point to a specific element")
                break
            current = revised
        assert verdict is not None
        return current, verdict, rounds

    pending = [i for i in issues if i["status"] == "pending"]
    reviewed = await asyncio.gather(*(review(i) for i in pending))   # concurrently; stored in issue order
    for issue, (current, verdict, rounds) in zip(pending, reviewed):
        for round_no, v in rounds:
            db.insert("judge_reviews", {"issue_id": issue["id"], "round": round_no, "verdict": v.verdict,
                                        "reasons_json": v.reasons, "created_at": utcnow()})
        status = "accepted" if verdict.verdict == "accept" else "rejected"
        counts[status] += 1
        db.update("issues", "id", issue["id"], {"status": status, "description": current["description"],
                                                "region_json": current.get("region_json"),
                                                "judge_json": {"verdict": verdict.verdict, "reasons": verdict.reasons}})
    return dict(counts)


async def replay_all(settings: Settings, db: Database, study: Study, profile: CalibrationProfile, run_id: str,
                     per_group: int = 2) -> dict[str, int]:
    sessions, steps, issues = _load(db, run_id)
    by_session = {s["id"]: s for s in sessions}
    personas = {p.id: p for p in load_personas(db, ids=sorted({s["persona_id"] for s in sessions}))}
    accepted = [i for i in issues if i["status"] == "accepted"]
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for issue in accepted:
        groups[group_key(issue)].append(issue)
    # Replay a few representative sessions per group; prefer sessions without simulated noise first.
    chosen: dict[str, list[dict]] = defaultdict(list)
    for members in groups.values():
        ranked = sorted(members, key=lambda i: bool((i["evidence_json"] or {}).get("noise_applied")))
        for issue in ranked[:per_group]:
            chosen[issue["session_id"]].append(issue)
    replayer = Replayer(settings, profile, study, sandbox=is_sandbox(study, settings))
    semaphore = asyncio.Semaphore(settings.run.concurrency)
    results: dict[str, dict[str, Any]] = {}
    async with BrowserPool(settings) as pool:
        async def one(session_id: str, members: list[dict]) -> None:
            async with semaphore:
                out = await replayer.replay(pool, personas[by_session[session_id]["persona_id"]],
                                            by_session[session_id], steps[session_id], members)
            results.update(out)
        await asyncio.gather(*(one(sid, members) for sid, members in chosen.items()))
    counts: dict[str, int] = defaultdict(int)
    for key, members in groups.items():
        replayed = [results[m["id"]] for m in members if m["id"] in results]
        group_status = ("CONFIRMED" if any(r["status"] == "CONFIRMED" for r in replayed)
                        else replayed[0]["status"] if replayed else "REPLAY_FAILED")
        for m in members:
            own = results.get(m["id"])
            status = own["status"] if own else group_status
            detail = own if own else {"status": group_status, "detail": "inherited from replays of the same issue"}
            db.update("issues", "id", m["id"], {"replay_status": status})
            db.insert("replays", {"issue_id": m["id"], "status": status, "hash_distance": detail.get("hash_distance"),
                                  "detail_json": detail})
            counts[status] += 1
    return dict(counts)


def label_novelty(db: Database, profile: CalibrationProfile, run_id: str) -> int:
    sessions, _, issues = _load(db, run_id)
    by_session = {s["id"]: s for s in sessions}
    personas = {p.id: p for p in load_personas(db, ids=sorted({s["persona_id"] for s in sessions}))}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for issue in issues:
        if issue["status"] == "accepted" and issue["failure_class"] == "UX_ISSUE":
            groups[group_key(issue)].append(issue)
    novel = 0
    for (task, category, screen), members in groups.items():
        is_novel, reason = classify(category, members, personas, by_session, profile.novelty)
        for m in members:
            judge = dict(m["judge_json"] or {})
            judge["novelty_reason"] = reason
            db.update("issues", "id", m["id"], {"novelty": is_novel, "judge_json": judge})
        novel += int(is_novel)
    return novel


async def verify_run(settings: Settings, db: Database, study: Study, profile: CalibrationProfile, run_id: str,
                     gateway: LLMGateway | None = None, replay: bool = True) -> dict[str, Any]:
    gateway = gateway or LLMGateway(settings, db, run_id=run_id)
    summary: dict[str, Any] = {"elicited": await elicit(settings, db, study, run_id, gateway)}
    summary["judge"] = await judge_all(settings, db, profile, run_id, gateway)
    summary["replay"] = await replay_all(settings, db, study, profile, run_id) if replay else {}
    summary["novelty_groups"] = label_novelty(db, profile, run_id)
    log.info("verification of %s: %s", run_id, summary)
    return summary
