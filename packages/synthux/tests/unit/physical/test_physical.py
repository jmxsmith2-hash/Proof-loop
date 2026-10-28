import asyncio
from pathlib import Path

import numpy as np

from synthux.personas.models import Persona, Traits
from synthux.physical import review_product, write_report
from synthux.physical.evaluate import Evidence, PhysicalFinding, judge_finding
from synthux.physical.guardrails import LABEL, sensory_claim
from synthux.physical.inputs import load_product, stl_bounding_box
from synthux.physical.reviews import Review, complaint_taxonomy
from synthux.physical.sensors import analyse, flat_lines, iqr_outliers, rolling_zscore_anomalies

EX = Path(__file__).resolve().parents[3] / "examples" / "physical"


def test_complaint_taxonomy_counts_complaints_not_praise():
    reviews = [Review("Way too heavy. Lid leaks."), Review("Not too heavy at all, never leaks."),
               Review("Easy to clean in the dishwasher."), Review("Doesn't fit my cup holder.", "A")]
    cats = {c.category: c for c in complaint_taxonomy(reviews)}
    assert cats["weight"].mentions == 1 and cats["leaks"].mentions == 1
    assert "cleaning" not in cats
    assert cats["size"].products == {"A": 1} and cats["weight"].share == 0.25


def test_stl_and_dimensions_are_read():
    dims = stl_bounding_box(EX / "trailflask.stl")
    assert dims == {"width": 92.0, "depth": 92.0, "height": 290.0}
    product = load_product(EX / "trailflask.yaml")
    assert product.spec["dimensions_mm"]["diameter"] == 92.0 and product.spec["mouth_diameter_mm"] == 48.0


def test_sensory_claims_are_blocked_but_specs_and_quotes_allowed():
    assert sensory_claim("It feels heavy in the hand")
    assert sensory_claim("the texture is rough") and sensory_claim("tastes metallic")
    assert sensory_claim("weight_g = 540 vs a median of 495") is None
    product = load_product(EX / "trailflask.yaml")
    ok = PhysicalFinding("weight", "Risk: weight complaints like “too heavy”",
                         "Reviews say “it smells after a week”; weight_g = 540 vs median 495", 0.5,
                         [Evidence("spec", "weight_g", {"key": "weight_g"})])
    assert judge_finding(ok, product).status == "accepted"   # quoted review words are evidence, not our claim
    bad = PhysicalFinding("ergonomics", "Grip", "The grip feels slippery and soft", 0.5,
                          [Evidence("photo", "trailflask_front.jpg", {})])
    assert judge_finding(bad, product).status == "rejected" and "sensory" in bad.reject_reason
    uncited = PhysicalFinding("value", "Value", "Probably too expensive", 0.5, [Evidence("review", "x", {"share": 0.0})])
    assert judge_finding(uncited, product).status == "rejected"


def test_sensor_analysis_finds_spike_stuck_sensor_and_usage():
    values = np.concatenate([np.linspace(4, 10, 200), [60.0], np.linspace(10, 12, 50)])
    assert 200 in rolling_zscore_anomalies(values) and 200 in iqr_outliers(values)
    assert flat_lines(np.array([1.0] * 30 + [2.0, 3.0]), min_run=20) == [(0, 29)]
    report = analyse(EX / "sensor_log.csv")
    kinds = {(f["kind"], f["column"]) for f in report.findings}
    assert ("anomaly", "water_temp_c") in kinds and ("flat_line", "water_temp_c") in kinds
    usage = next(f for f in report.findings if f["kind"] == "usage" and f["column"] == "lid_open")
    assert usage["sessions"] == 9 and usage["busiest_hour_utc"] in (8, 12, 17)


def test_full_physical_review(db, tmp_path):
    product = load_product(EX / "trailflask.yaml")
    personas = [Persona(id="per_a", label="Budget shopper", narrative="", traits=Traits(budget="tight")),
                Persona(id="per_b", label="Arthritis", narrative="", traits=Traits(motor="limited_dexterity", age_range="65+"))]
    result = asyncio.run(review_product(db, product, personas))
    assert result["reviews"] == 26
    risks = {r["category"]: r for r in result["summary"]}
    assert result["return_reasons"]["per_a"]["category"] == "size"  # 92 mm vs similar products; complaints rise with size
    assert risks["size"]["evidence"][1]["data"]["on_complaint_side"] is True
    assert risks["weight"]["evidence"][1]["data"]["key"] == "weight_g"
    assert result["return_reasons"]["per_b"]["category"] in ("ergonomics", "weight")  # motor + age traits weigh in
    for findings in result["per_persona"].values():
        assert all(f.detail.startswith(LABEL) for f in findings)
    page = write_report(result, tmp_path, personas).read_text(encoding="utf-8")
    assert "PREDICTION, NOT A TEST" in page and "Kevin" not in page and "kb@example.com" not in page
    assert db.all("SELECT * FROM sensor_findings") and db.all("SELECT * FROM review_complaints")
