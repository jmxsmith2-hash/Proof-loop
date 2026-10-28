"""Build the static HTML report for one run."""

from __future__ import annotations

import shutil
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape
from PIL import Image, ImageDraw

from synthux import SYNTHETIC_DISCLAIMER, __version__
from synthux.aggregate import aggregate_run, cluster_summary
from synthux.aggregate.dedupe import Cluster
from synthux.aggregate.guardrails import strip_demand_predictions
from synthux.core.config import Settings
from synthux.core.db import Database, utcnow
from synthux.core.llm import LLMGateway, LLMRequest
from synthux.core.logging import get_logger
from synthux.personas.diversity import feedback_pairs
from synthux.personas.generate import load_personas
from synthux.personas.models import Persona
from synthux.report.charts import bar_chart, legend, line_chart

log = get_logger(__name__)

LIMITATIONS = [
    "Personas are simulations. They miss emotions, context and motivations that real people bring.",
    "Findings for disability, age and cultural personas are low confidence: verify with real users.",
    "The default engine reads the screen with OCR and acts by rules. It catches structural problems "
    "(visibility, errors, forms, speed) well and judges visual appeal or content quality poorly.",
    "Nothing here predicts sales, demand or conversion. Price questions only show where price became an objection.",
    "A clean result means no persona hit a problem. It does not mean there are none.",
]


def _evidence_image(member: dict[str, Any], out: Path, name: str) -> dict[str, str] | None:
    """Full screenshot with the problem outlined, plus a zoomed crop around it."""
    shot = (member.get("evidence_json") or {}).get("screenshot")
    if not shot or not Path(shot).is_file():
        return None
    img = Image.open(shot).convert("RGB")
    region = member.get("region_json")
    out.mkdir(parents=True, exist_ok=True)
    crop_rel = None
    if region:
        draw = ImageDraw.Draw(img)
        x, y, w, h = region["x"], region["y"], region["w"], region["h"]
        for k in range(3):
            draw.rectangle([x - 4 - k, y - 4 - k, x + w + 4 + k, y + h + 4 + k], outline=(220, 38, 38))
        pad = 170
        box = (max(0, x - pad), max(0, y - pad), min(img.width, x + w + pad), min(img.height, y + h + pad))
        img.crop(box).save(out / f"{name}_zoom.jpg", "JPEG", quality=85)
        crop_rel = f"assets/{name}_zoom.jpg"
    img.save(out / f"{name}.jpg", "JPEG", quality=80)
    return {"full": f"assets/{name}.jpg", "zoom": crop_rel or f"assets/{name}.jpg"}


def _representative(c: Cluster) -> dict[str, Any]:
    def rank(m: dict[str, Any]) -> tuple:
        return (m.get("replay_status") == "CONFIRMED", m["source"] == "behavior", m["severity"],
                bool(m.get("region_json")))
    return max(c.members, key=rank)


def _timeline(session: dict[str, Any]) -> list[float]:
    return list((session.get("memory_json") or {}).get("frustration_timeline", []))


def _avg_timeline(sessions: list[dict[str, Any]]) -> list[float]:
    lines = [_timeline(s) for s in sessions if _timeline(s)]
    if not lines:
        return []
    longest = max(len(t) for t in lines)
    padded = [t + [t[-1]] * (longest - len(t)) for t in lines]
    return [round(mean(col), 1) for col in zip(*padded)]


class ReportBuilder:
    def __init__(self, settings: Settings, db: Database, gateway: LLMGateway | None = None):
        self.settings = settings
        self.db = db
        self.gateway = gateway
        self.profile = None
        self.env = Environment(loader=PackageLoader("synthux", "report/templates"),
                               autoescape=select_autoescape(["html", "j2"]), trim_blocks=True, lstrip_blocks=True)

    async def build(self, run_id: str, out_dir: Path | None = None) -> Path:
        run = self.db.one("SELECT * FROM runs WHERE id = ?", [run_id])
        if run is None:
            raise KeyError(f"no run {run_id}")
        out_dir = Path(out_dir or self.settings.data_dir / "reports" / run_id)
        if out_dir.exists():
            shutil.rmtree(out_dir)
        assets = out_dir / "assets"
        clusters = await aggregate_run(self.db, run_id, self.settings.run.promote_min, self.gateway)
        from synthux.calibration.profiles import load_profile

        try:
            self.profile = load_profile(self.settings, self.db, run["calibration_version"])
        except Exception:  # profile file removed since the run; charts just omit thresholds
            self.profile = None
        sessions = self.db.all("SELECT * FROM sessions WHERE run_id = ? ORDER BY rowid", [run_id])
        personas = {p.id: p for p in load_personas(self.db, ids=sorted({s["persona_id"] for s in sessions}))}
        data = self._data(run, sessions, personas, clusters, assets)
        data["summary_text"] = await self._summary_text(data)
        html = self.env.get_template("report.html.j2").render(**data)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(html, encoding="utf-8")
        log.info("report written to %s", out_dir / "index.html")
        return out_dir / "index.html"

    # ------------------------------------------------------------------ data
    def _data(self, run: dict, sessions: list[dict], personas: dict[str, Persona], clusters: list[Cluster],
              assets: Path) -> dict[str, Any]:
        promoted = [c for c in clusters if c.detail["promoted"] and not c.detail["novelty"]]
        top = []
        for n, c in enumerate(promoted[:10]):
            rep = _representative(c)
            top.append({**cluster_summary(c), "rank": n + 1, "image": _evidence_image(rep, assets, c.id),
                        "description": rep["description"],
                        "steps": (rep.get("evidence_json") or {}).get("steps", []),
                        "think": self._think(rep),
                        "persona_labels": [personas[p].label for p in c.detail["personas"] if p in personas],
                        "judge": (rep.get("judge_json") or {}).get("reasons", [])})
        issues = self.db.all("SELECT * FROM issues WHERE run_id = ?", [run["id"]])
        return {
            "disclaimer": SYNTHETIC_DISCLAIMER, "version": __version__, "generated": utcnow(), "run": run,
            "config": run["config_json"], "top": top,
            "more": [cluster_summary(c) for c in promoted[10:]],
            "novelty": [cluster_summary(c) for c in clusters if c.detail["promoted"] and c.detail["novelty"]],
            "unpromoted": [cluster_summary(c) for c in clusters if not c.detail["promoted"]],
            "limitations_found": self._limitations(issues),
            "judge_stats": self._judge_stats(issues),
            "rejected_examples": [i for i in issues if i["status"] == "rejected"][:8],
            "completion": self._completion(sessions, personas),
            "quits": self._quits(sessions, personas),
            "timelines": self._timelines(sessions, personas),
            "prices": self._prices(run["id"]),
            "learnability": self._learnability(sessions, personas),
            "roster": self._roster(personas, sessions),
            "diversity": self._diversity(run["id"], personas),
            "limitations": LIMITATIONS,
            "counts": {"sessions": len(sessions), "personas": len(personas),
                       "completed": sum(1 for s in sessions if s["outcome"] == "completed"),
                       "quit": sum(1 for s in sessions if s["outcome"] in ("quit", "budget_exhausted")),
                       "agent_failures": sum(1 for s in sessions if s["outcome"] in ("agent_failure", "blocked")),
                       "promoted": len(promoted)},
        }

    def _think(self, member: dict[str, Any]) -> dict[str, Any]:
        """Think-aloud at the moment of the problem (the persona's reaction, for errors and slow loads)."""
        evidence = member.get("evidence_json") or {}
        step = evidence.get("think_step")
        if step is not None:
            row = self.db.one("SELECT think_json FROM steps WHERE session_id = ? AND idx = ?", [member["session_id"], step])
            if row:
                return row["think_json"]
        return evidence.get("think_aloud", {})

    def _completion(self, sessions: list[dict], personas: dict[str, Persona]) -> dict[str, Any]:
        by_type: dict[str, list[dict]] = defaultdict(list)
        table: dict[tuple[str, str], list[dict]] = defaultdict(list)
        tasks = sorted({s["task_id"] for s in sessions})
        for s in sessions:
            p = personas.get(s["persona_id"])
            label = p.type_label() if p else s["persona_id"]
            by_type[label].append(s)
            table[(label, s["task_id"])].append(s)
        rows = []
        for label, rows_ in sorted(by_type.items()):
            done = sum(1 for s in rows_ if s["outcome"] == "completed")
            rows.append((label, done / len(rows_), f"{label}: {done} of {len(rows_)} sessions completed"))
        grid = [{"type": label, "cells": [
            (lambda ss: {"done": sum(1 for s in ss if s["outcome"] == "completed"), "n": len(ss)})(table[(label, t)])
            for t in tasks]} for label in sorted(by_type)]
        return {"chart": bar_chart(rows, width=960), "tasks": tasks, "grid": grid}

    def _quits(self, sessions: list[dict], personas: dict[str, Persona]) -> list[dict[str, Any]]:
        out = []
        for task in sorted({s["task_id"] for s in sessions}):
            ss = [s for s in sessions if s["task_id"] == task]
            quits = [s for s in ss if s["outcome"] in ("quit", "budget_exhausted")]
            where: Counter[str] = Counter()
            reasons: dict[str, list[str]] = defaultdict(list)
            for s in quits:
                last = self.db.one("SELECT screen_id FROM steps WHERE session_id = ? ORDER BY idx DESC LIMIT 1", [s["id"]])
                screen = last["screen_id"] if last else "?"
                where[screen] += 1
                label = personas[s["persona_id"]].label if s["persona_id"] in personas else s["persona_id"]
                reasons[screen].append(f"{label}: “{s['quit_reason']}”")
            rows = [(screen, n / max(1, len(ss)), f"{n} of {len(ss)} sessions quit on {screen}") for screen, n in where.most_common()]
            out.append({"task": task, "started": len(ss), "quit": len(quits),
                        "completed": sum(1 for s in ss if s["outcome"] == "completed"),
                        "chart": bar_chart(rows, width=460, label_width=110) if rows else "",
                        "where": [{"screen": sc, "count": n, "reasons": sorted(set(reasons[sc]))[:6]}
                                  for sc, n in where.most_common()]})
        return out

    def _timelines(self, sessions: list[dict], personas: dict[str, Persona]) -> list[dict[str, Any]]:
        order = {pid: i for i, pid in enumerate(sorted(personas))}  # colour follows the persona everywhere
        out = []
        for task in sorted({s["task_id"] for s in sessions}):
            series = []
            for pid in sorted({s["persona_id"] for s in sessions if s["task_id"] == task}, key=lambda p: order.get(p, 99)):
                ss = [s for s in sessions if s["task_id"] == task and s["persona_id"] == pid and s["visit_index"] == 0]
                p = personas.get(pid)
                threshold = None
                if p is not None and self.profile is not None:
                    threshold = float(self.profile.frustration.threshold_by_patience.get(p.traits.patience, 65))
                    threshold *= 0.85 if p.traits.time_pressure else 1.0
                series.append({"name": p.label if p else pid, "slot": order.get(pid, 0), "values": _avg_timeline(ss),
                               "outcomes": Counter(s["outcome"] for s in ss), "threshold": threshold})
            if len(series) > 4:
                for one in series:
                    one["threshold"] = None  # too busy; thresholds are listed in the table instead
            out.append({"task": task, "chart": line_chart(series, title=f"frustration over steps: {task}"),
                        "legend": legend(series), "series": series})
        return out

    def _prices(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT q.answers_json, s.task_id FROM questionnaires q JOIN sessions s ON s.id = q.session_id "
                           "WHERE s.run_id = ?", [run_id])
        objections: dict[tuple[str, float], set[str]] = defaultdict(set)
        for r in rows:
            for choice in r["answers_json"].get("price_choices", []):
                if choice["choice"] != "buy now":
                    objections[(r["task_id"], float(choice["price"]))].add(strip_demand_predictions(choice["reason"]))
        return [{"task": t, "price": p, "objections": sorted(reasons)} for (t, p), reasons in sorted(objections.items())]

    def _learnability(self, sessions: list[dict], personas: dict[str, Persona]) -> list[dict[str, Any]]:
        pairs: dict[tuple[str, str], dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for s in sessions:
            pairs[(s["persona_id"], s["task_id"])][s["visit_index"]].append(s)
        out = []
        for (pid, task), visits in sorted(pairs.items()):
            if len(visits) < 2:
                continue
            first, later = visits[0], visits[max(visits)]
            out.append({"persona": personas[pid].label if pid in personas else pid, "task": task,
                        "steps_first": round(mean(s["steps_count"] for s in first), 1),
                        "steps_later": round(mean(s["steps_count"] for s in later), 1),
                        "frustration_first": round(mean(s["final_frustration"] for s in first), 1),
                        "frustration_later": round(mean(s["final_frustration"] for s in later), 1)})
        return out

    def _roster(self, personas: dict[str, Persona], sessions: list[dict]) -> list[dict[str, Any]]:
        return [{"id": p.id, "label": p.label, "traits": p.traits.model_dump(), "narrative": p.narrative,
                 "low_confidence": p.low_confidence, "note": p.confidence_note, "source": p.source_summary,
                 "engine": p.engine or self.settings.engine.engine_for("actor"),
                 "sessions": sum(1 for s in sessions if s["persona_id"] == p.id)}
                for p in sorted(personas.values(), key=lambda p: p.id)]

    def _diversity(self, run_id: str, personas: dict[str, Persona]) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT x.persona_id, s.think_json FROM steps s JOIN sessions x ON x.id = s.session_id "
                           "WHERE x.run_id = ?", [run_id])
        text: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            t = r["think_json"]
            text[r["persona_id"]].append(f"{t.get('expects', '')} {t.get('feels', '')}")
        pairs = feedback_pairs({k: " ".join(v) for k, v in text.items()}, threshold=0.92)
        return [{"a": personas[p.a].label if p.a in personas else p.a,
                 "b": personas[p.b].label if p.b in personas else p.b, "score": p.score} for p in pairs]

    @staticmethod
    def _limitations(issues: list[dict]) -> list[dict[str, Any]]:
        counts: Counter[tuple[str, str, str]] = Counter()
        for i in issues:
            if i["failure_class"] != "UX_ISSUE":
                counts[(i["failure_class"], i["category"], i["description"][:120])] += 1
        return [{"class": k[0], "category": k[1], "detail": k[2], "count": n} for k, n in counts.most_common()]

    @staticmethod
    def _judge_stats(issues: list[dict]) -> dict[str, Any]:
        ux = [i for i in issues if i["failure_class"] == "UX_ISSUE"]
        reasons: Counter[str] = Counter()
        for i in ux:
            if i["status"] == "rejected":
                first = ((i.get("judge_json") or {}).get("reasons") or ["?"])[0]
                reasons[first.split(":")[0]] += 1
        replay = Counter(i.get("replay_status") or "not replayed" for i in ux if i["status"] == "accepted")
        return {"reported": len(ux), "accepted": sum(1 for i in ux if i["status"] == "accepted"),
                "rejected": sum(1 for i in ux if i["status"] == "rejected"),
                "stated": sum(1 for i in ux if i["source"] == "elicited"),
                "reject_reasons": reasons.most_common(), "replay": sorted(replay.items())}

    async def _summary_text(self, data: dict[str, Any]) -> str:
        """Plain-language summary. Template text by default; the reporter model when configured."""
        top = data["top"]
        if not top:
            base = ("No issue recurred and survived the judge and replay. That is not proof the product is free of "
                    "problems; the personas may simply not have reached them.")
        else:
            first = top[0]
            base = (f"{data['counts']['promoted']} issues survived judging, replay and repetition. The most serious: "
                    f"“{first['title']}” on {first['screen']} ({first['severity_name']}, seen in "
                    f"{first['sessions_with']} of {first['sessions_total']} sessions by {first['personas_affected']} "
                    f"persona(s)). {data['counts']['quit']} of {data['counts']['sessions']} sessions ended with the "
                    f"persona giving up.")
        if self.gateway is None or not self.gateway.is_model_backed("reporter"):
            return base
        schema = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}
        facts = [{"title": t["title"], "screen": t["screen"], "severity": t["severity_name"],
                  "sessions": f"{t['sessions_with']}/{t['sessions_total']}", "confidence": t["confidence"]} for t in top]
        prompt = ("Write a 4-6 sentence plain-language summary of these synthetic usability findings for a product "
                  "team. Do not predict sales, demand or conversion. Do not add findings.\n" + str(facts))
        data_out = (await self.gateway.complete(LLMRequest(role="reporter", system="You summarise usability research.",
                                                           prompt=prompt, schema=schema, max_tokens=800))).data
        return strip_demand_predictions(data_out["summary"])
