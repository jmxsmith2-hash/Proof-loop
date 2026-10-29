"""Personas evaluate a physical product against real complaint patterns from similar products.

Same rules as the web module: behavioural, forced-choice questions ("which
complaint would make you return it?"), at least one specific claim per
complaint category, every claim cites evidence (a spec value, a review pattern
or a photo region), and a judge rejects anything unsupported, flattering or sensory.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from PIL import Image

from proofloop.check.guardrails import LABEL, sensory_claim
from proofloop.check.inputs import ProductSpec, flat_spec
from proofloop.check.reviews import Complaint
from proofloop.core.llm import LLMGateway, LLMRequest
from proofloop.core.logging import get_logger
from proofloop.personas.models import Persona

log = get_logger(__name__)

SPEC_KEYS = {
    "size": ["dimensions_mm.diameter", "dimensions_mm.width", "dimensions_mm.height", "dimensions_mm.depth"],
    "weight": ["weight_g"],
    "value": ["price_usd"],
    "performance": ["insulation_hours", "battery_hours", "runtime_hours"],
    "durability": ["warranty_years", "wall_thickness_mm"],
    "cleaning": ["mouth_diameter_mm"],
}
LOWER_IS_WORSE = {"performance", "durability", "cleaning"}  # fewer hours / thinner walls / narrower mouth = worse
MIN_SHARE = 0.05


@dataclass
class Evidence:
    kind: str                 # review | spec | photo
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhysicalFinding:
    category: str
    title: str
    detail: str
    risk: float               # 0..1
    evidence: list[Evidence]
    persona_id: str | None = None
    status: str = "pending"   # accepted | rejected
    reject_reason: str = ""

    @property
    def risk_label(self) -> str:
        return "high" if self.risk >= 0.6 else "medium" if self.risk >= 0.3 else "low"


def _relevance(category: str, persona: Persona) -> float:
    t = persona.traits
    factor = 1.0
    if category == "value":
        factor *= {"tight": 1.5, "moderate": 1.0, "generous": 0.6}[t.budget]
    if category in ("ergonomics", "weight") and (t.motor != "none" or t.age_range == "65+"):
        factor *= 1.5
    if category == "instructions" and (t.tech_skill <= 2 or t.eyesight != "normal"):
        factor *= 1.4
    if category in ("durability", "leaks") and t.patience <= 2:
        factor *= 1.2
    if category == "cleaning" and t.time_pressure:
        factor *= 1.2
    return factor


def spec_comparison(category: str, product: ProductSpec, complaint: Complaint) -> Evidence | None:
    """Compare the product's spec with similar products, oriented by which side attracts complaints."""
    ours = flat_spec(product.spec)
    for key in SPEC_KEYS.get(category, []):
        if key not in ours:
            continue
        others = [(p.name, flat_spec(p.spec).get(key)) for p in product.similar_products]
        others = [(n, v) for n, v in others if v is not None]
        if len(others) < 2:
            return Evidence("spec", f"{key} is {ours[key]:g}; not enough similar products to compare", {"key": key})
        values = [v for _, v in others]
        mid = median(values)
        rates = [complaint.products.get(n, 0) for n, _ in others]
        rho = spearman(values, [float(r) for r in rates]) if len(others) >= 3 else None
        if rho is not None and abs(rho) >= 0.3:
            worse_high = rho > 0
            basis = f"complaint rates rise with {key} (rank correlation {rho:+.2f})" if worse_high else \
                    f"complaint rates rise as {key} falls (rank correlation {rho:+.2f})"
        else:
            worse_high = category not in LOWER_IS_WORSE
            basis = "direction assumed from the complaint wording"
        on_bad_side = ours[key] > mid if worse_high else ours[key] < mid
        ratio = ours[key] / mid if mid else 1.0
        return Evidence("spec", f"{key} = {ours[key]:g} vs a median of {mid:g} across {len(others)} similar "
                                f"products; {basis}", {"key": key, "value": ours[key], "median": mid,
                                                      "on_complaint_side": on_bad_side, "ratio": round(ratio, 3)})
    return None


class HeuristicPhysicalEvaluator:
    def evaluate(self, product: ProductSpec, complaints: list[Complaint], persona: Persona) -> list[PhysicalFinding]:
        findings = []
        for c in complaints:
            if c.share < MIN_SHARE:
                continue
            evidence = [Evidence("review", f"{c.share:.0%} of reviews of similar products complain about {c.category}",
                                 {"share": c.share, "mentions": c.mentions, "quotes": c.quotes[:2]})]
            spec = spec_comparison(c.category, product, c)
            risk = min(1.0, c.share * 2.5) * _relevance(c.category, persona)
            if spec is not None:
                evidence.append(spec)
                if spec.data.get("on_complaint_side") is True:
                    risk *= 1.0 + min(0.8, abs(spec.data.get("ratio", 1) - 1) * 2)
                elif spec.data.get("on_complaint_side") is False:
                    risk *= 0.5
            phrase = max(c.phrases, key=c.phrases.get) if c.phrases else c.category
            detail = (f"Reviews of similar products often say “{phrase}” ({c.mentions} reviews). "
                      + (spec.detail + ". " if spec else "No spec value to compare, so this risk is carried over "
                                                         "from similar products. "))
            findings.append(PhysicalFinding(c.category, f"Risk: {c.category} complaints like “{phrase}”",
                                            detail, round(min(1.0, risk), 3), evidence, persona.id))
        return findings

    @staticmethod
    def return_reason(findings: list[PhysicalFinding]) -> dict[str, Any] | None:
        """Forced choice: which complaint would most likely make this persona return the product?"""
        accepted = [f for f in findings if f.status == "accepted"]
        if not accepted:
            return None
        top = max(accepted, key=lambda f: f.risk)
        return {"category": top.category, "risk": top.risk, "because": top.detail}


PHYSICAL_SCHEMA = {
    "type": "object",
    "required": ["findings", "return_reason"],
    "properties": {
        "findings": {"type": "array", "items": {"type": "object",
                     "required": ["category", "claim", "evidence_type", "evidence_ref", "risk"],
                     "properties": {"category": {"type": "string"}, "claim": {"type": "string"},
                                    "evidence_type": {"type": "string", "enum": ["spec", "review", "photo"]},
                                    "evidence_ref": {"type": "string"},
                                    "risk": {"type": "number", "minimum": 0, "maximum": 1}}}},
        "return_reason": {"type": "string"},
    },
}


class ModelPhysicalEvaluator:
    """A model persona looks at the photos and specs, grounded in the review complaint patterns."""

    def __init__(self, gateway: LLMGateway):
        self.gateway = gateway

    async def evaluate(self, product: ProductSpec, complaints: list[Complaint], persona: Persona) -> list[PhysicalFinding]:
        images = []
        for photo in product.photos[:4]:
            buf = io.BytesIO()
            Image.open(product.path(photo)).convert("RGB").save(buf, "PNG")
            images.append(buf.getvalue())
        patterns = [{"category": c.category, "share": c.share, "quotes": c.quotes[:2]} for c in complaints]
        prompt = (f"You are this person: {persona.narrative}\nProduct (maker not named): {product.category}\n"
                  f"Spec: {product.spec}\nComplaint patterns from REAL reviews of similar products: {patterns}\n"
                  "For each complaint category, say whether this product is likely to get the same complaint from "
                  "you, citing a spec value, a review pattern or something visible in a photo (say which photo and "
                  "where). You cannot touch, lift, taste or smell it: never claim how it feels, how heavy it feels, "
                  "its texture, taste or smell. Then: which complaint would most likely make you return it?")
        data = (await self.gateway.complete(LLMRequest(role="persona", system="You predict product complaints.",
                                                       prompt=prompt, schema=PHYSICAL_SCHEMA, images=images,
                                                       max_tokens=2000))).data
        out = []
        for f in data["findings"]:
            share = next((c.share for c in complaints if c.category == f["category"]), 0.0)
            out.append(PhysicalFinding(f["category"], f"Risk: {f['category']}", f["claim"], float(f["risk"]),
                                       [Evidence(f["evidence_type"], f["evidence_ref"], {"share": share})], persona.id))
        return out


def judge_finding(finding: PhysicalFinding, product: ProductSpec) -> PhysicalFinding:
    """Reject sensory, uncited or flattering predictions."""
    own_words = re.sub("\u201c[^\u201d]*\u201d|\"[^\"]*\"", "", f"{finding.title} {finding.detail}")  # quotes are evidence
    if (word := sensory_claim(own_words)):
        finding.status, finding.reject_reason = "rejected", f"claims a sensory judgement ('{word}')"
        return finding
    if not finding.evidence:
        finding.status, finding.reject_reason = "rejected", "no evidence cited"
        return finding
    valid = []
    for e in finding.evidence:
        if e.kind == "review" and e.data.get("share", 0) >= MIN_SHARE:
            valid.append(e)
        elif e.kind == "spec" and ("key" in e.data or any(k in e.detail for k in flat_spec(product.spec))):
            valid.append(e)
        elif e.kind == "photo" and any(p in e.detail for p in product.photos):
            valid.append(e)
    if not valid:
        finding.status, finding.reject_reason = "rejected", "evidence does not point to a spec value, review pattern or photo"
        return finding
    if any(w in finding.detail.lower() for w in ("great product", "love it", "perfect", "excellent")):
        finding.status, finding.reject_reason = "rejected", "flattering, not a prediction"
        return finding
    finding.status = "accepted"
    return finding


def label_findings(findings: list[PhysicalFinding]) -> list[PhysicalFinding]:
    for f in findings:
        if not f.detail.startswith(LABEL):
            f.detail = f"{LABEL}. {f.detail}"
    return findings


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
    """Rank correlation of two lists, or None when there is too little to rank."""
    if len(a) < 3 or len(set(a)) < 2 or len(set(b)) < 2:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=False))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return round(num / den, 3) if den else None
