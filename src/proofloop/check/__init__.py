"""Physical products (secondary module): predictive feedback, clearly labelled as prediction, not a test."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from proofloop.check.evaluate import (
    HeuristicPhysicalEvaluator,
    ModelPhysicalEvaluator,
    PhysicalFinding,
    judge_finding,
    label_findings,
)
from proofloop.check.inputs import ProductSpec
from proofloop.check.reviews import complaint_taxonomy, load_reviews
from proofloop.check.sensors import analyse
from proofloop.core.db import Database, new_id, utcnow
from proofloop.core.llm import LLMGateway
from proofloop.core.logging import get_logger
from proofloop.personas.models import Persona

log = get_logger(__name__)


async def review_product(db: Database, product: ProductSpec, personas: list[Persona],
                         gateway: LLMGateway | None = None) -> dict[str, Any]:
    product_id = new_id("prd")
    reviews = load_reviews([product.path(r) for r in product.reviews])
    complaints = complaint_taxonomy(reviews)
    db.insert("physical_products", {"id": product_id, "name": product.name, "created_at": utcnow(),
                                    "spec_json": product.spec,
                                    "inputs_json": {"photos": product.photos, "reviews": product.reviews,
                                                    "cad": product.cad, "sensors": product.sensors,
                                                    "similar": [p.name for p in product.similar_products],
                                                    "review_count": len(reviews)}})
    for c in complaints:
        db.insert("review_complaints", {"product_id": product_id, "category": c.category, "mentions": c.mentions,
                                        "share": c.share, "quotes_json": c.quotes})
    heuristic = HeuristicPhysicalEvaluator()
    model = ModelPhysicalEvaluator(gateway) if gateway is not None and gateway.is_model_backed("persona") else None
    per_persona: dict[str, list[PhysicalFinding]] = {}
    for persona in personas:
        found = await model.evaluate(product, complaints, persona) if model else heuristic.evaluate(product, complaints, persona)
        per_persona[persona.id] = label_findings([judge_finding(f, product) for f in found])
        for f in per_persona[persona.id]:
            db.insert("physical_findings", {"product_id": product_id, "persona_id": persona.id, "category": f.category,
                                            "title": f.title, "detail": f.detail,
                                            "evidence_json": [asdict(e) for e in f.evidence] + [{"risk": f.risk}],
                                            "status": f.status if f.status == "accepted" else f"rejected: {f.reject_reason}"})
    sensors = []
    for path in product.sensors:
        report = analyse(product.path(path))
        sensors.append(report)
        for finding in report.findings:
            db.insert("sensor_findings", {"product_id": product_id, "source_file": report.source, "kind": finding["kind"],
                                          "detail_json": finding})
    summary = _summarise(per_persona, personas)
    log.info("physical review %s: %d reviews, %d complaint categories, %d personas", product_id, len(reviews),
             len(complaints), len(personas))
    return {"product_id": product_id, "product": product, "reviews": len(reviews), "complaints": complaints,
            "per_persona": per_persona, "summary": summary, "sensors": sensors,
            "return_reasons": {p.id: HeuristicPhysicalEvaluator.return_reason(per_persona[p.id]) for p in personas}}


def _summarise(per_persona: dict[str, list[PhysicalFinding]], personas: list[Persona]) -> list[dict[str, Any]]:
    by_cat: dict[str, list[PhysicalFinding]] = {}
    for findings in per_persona.values():
        for f in findings:
            if f.status == "accepted":
                by_cat.setdefault(f.category, []).append(f)
    labels = {p.id: p for p in personas}
    rows = []
    for cat, fs in by_cat.items():
        risk = sum(f.risk for f in fs) / len(fs)
        worst = max(fs, key=lambda f: f.risk)
        flagged = [labels[f.persona_id] for f in fs if f.risk >= 0.6 and f.persona_id in labels]
        rows.append({"category": cat, "risk": round(risk, 3),
                     "risk_label": "high" if risk >= 0.6 else "medium" if risk >= 0.3 else "low",
                     "title": worst.title, "detail": worst.detail, "evidence": [asdict(e) for e in worst.evidence],
                     "personas_high": [p.label for p in flagged],
                     "low_confidence": bool(flagged) and all(p.low_confidence for p in flagged)})
    return sorted(rows, key=lambda r: -r["risk"])


def write_report(result: dict[str, Any], out_dir: Path, personas: list[Persona]) -> Path:
    from proofloop import SYNTHETIC_DISCLAIMER
    from proofloop.check.guardrails import LABEL
    from proofloop.core import theme

    env = theme.environment("proofloop.check", "templates")
    html = env.get_template("physical.html.j2").render(
        r=result, label=LABEL, disclaimer=SYNTHETIC_DISCLAIMER, personas={p.id: p for p in personas},
        generated=utcnow())
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path
