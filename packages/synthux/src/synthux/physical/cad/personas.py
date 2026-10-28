"""Personas react to a CAD design: grounded only in measured facts (prediction, not a test).

A general complaint pattern (data/product_knowledge.yaml) becomes a persona
finding only when a measured fact supports it for that persona (their hand
size, grip strength, body size, motor or vision traits). Patterns with no
measurable fact are listed as a checklist, never as findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

from synthux.personas.models import Persona
from synthux.physical.cad.ergonomics import anthropometrics
from synthux.physical.guardrails import LABEL, sensory_claim

TYPE_ALIASES = {"desk accessory": "everyday", "household": "everyday", "kitchen": "everyday", "toy": "everyday",
                "tool": "everyday", "gadget": "electronics", "enclosure": "electronics", "case": "electronics",
                "clothing": "wearable", "jewellery": "wearable", "jewelry": "wearable", "3d printed": "3d_printed",
                "3d-printed": "3d_printed"}


@lru_cache(maxsize=1)
def knowledge() -> dict[str, list[dict[str, Any]]]:
    return yaml.safe_load((resources.files("synthux") / "data" / "product_knowledge.yaml").read_text(encoding="utf-8"))


def product_types(declared: list[str] | str | None, use: dict[str, Any]) -> list[str]:
    raw = [declared] if isinstance(declared, str) else list(declared or [])
    types = []
    for t in raw + ["3d_printed"]:
        key = TYPE_ALIASES.get(str(t).lower(), str(t).lower())
        if key in knowledge() and key not in types:
            types.append(key)
    if use.get("handheld") and "handheld" not in types:
        types.append("handheld")
    if use.get("wearable") and "wearable" not in types:
        types.append("wearable")
    if not any(t in types for t in ("everyday", "electronics", "wearable")):
        types.insert(0, "everyday")
    return types


@dataclass
class PersonaReaction:
    persona_id: str
    pattern: str
    title: str
    text: str                      # first person, labelled as a prediction
    risk: float                    # 0..1
    evidence: list[str] = field(default_factory=list)


def _body_size(p: Persona) -> str:
    return p.traits.hand_size      # hands are a reasonable proxy for overall body size here


def _affects(p: Persona, tags: list[str]) -> bool:
    t = p.traits
    checks = {"all": True, "small_hands": t.hand_size == "small", "large_hands": t.hand_size == "large",
              "low_grip": t.grip_strength == "low", "motor": t.motor != "none", "low_vision": t.eyesight != "normal",
              "small_body": t.hand_size == "small", "large_body": t.hand_size == "large"}
    return any(checks.get(tag, False) for tag in tags)


def _severity_risk(sev: int) -> float:
    return {1: 0.2, 2: 0.45, 3: 0.7, 4: 0.95}[sev]


def react(persona: Persona, facts: dict[str, Any], types: list[str]) -> list[PersonaReaction]:
    """Every reaction cites a measured fact; nothing is invented."""
    data = anthropometrics()
    t = persona.traits
    findings = facts["findings"]
    hand = data["hand"][t.hand_size]
    mass_limit = hand["one_hand_mass_g"] * data["grip_strength_factor"][t.grip_strength]
    if t.age_range == "65+" or t.motor != "none":
        mass_limit *= 0.8
    out: list[PersonaReaction] = []
    for kind in types:
        for pattern in knowledge().get(kind, []):
            if not pattern["facts"] or not _affects(persona, pattern["affects"]):
                continue
            reaction = _evaluate(pattern, persona, facts, findings, hand, mass_limit)
            if reaction is not None and not sensory_claim(reaction.text):
                out.append(reaction)
    unique: dict[str, PersonaReaction] = {}
    for r in out:
        if r.pattern not in unique or r.risk > unique[r.pattern].risk:
            unique[r.pattern] = r
    return sorted(unique.values(), key=lambda r: -r.risk)


def _evaluate(pattern: dict, persona: Persona, facts: dict, findings: dict, hand: dict,
              mass_limit: float) -> PersonaReaction | None:
    pid, title = persona.id, pattern["title"]

    def from_finding(check: str, text: str) -> PersonaReaction | None:
        f = findings.get(check)
        if f is None or f.severity < 2:
            return None
        loc = f" at x={f.location['x']}, y={f.location['y']}, z={f.location['z']} mm" if f.location else ""
        return PersonaReaction(pid, pattern["id"], title, f"{LABEL}. {text}", _severity_risk(f.severity),
                               [f"design check '{f.title}'{loc}: {f.detail}"])

    for check in pattern["facts"]:
        if check in ("stability", "sharp_edge", "thin_wall", "weak_layers", "overhang", "clearance", "warping",
                     "connector_opening", "button_size"):
            texts = {
                "stability": "I'd expect it to tip over in normal use; that alone would put me off.",
                "sharp_edge": "Those sharp edges would bother me every time I pick it up.",
                "thin_wall": "That thin section looks like the first thing to break.",
                "weak_layers": "I'd expect that upright piece to snap off along the layers.",
                "overhang": "Surfaces under the overhang usually print rough or droop; I'd notice that.",
                "clearance": "If the parts fuse or don't fit together, I can't use it as intended.",
                "warping": "If the base warps it will rock on the table.",
                "connector_opening": "If my cable doesn't click in all the way, I'd return it.",
                "button_size": "Buttons that small are hard for me to press reliably.",
            }
            r = from_finding(check, texts[check])
            if r and check == "button_size" and not (persona.traits.motor != "none" or persona.traits.hand_size == "large"
                                                    or persona.traits.eyesight != "normal"):
                r.risk *= 0.5
            if r:
                return r
        if check == "mass_vs_hand" and facts.get("handheld") and facts["mass_g"] > mass_limit:
            return PersonaReaction(pid, pattern["id"], title,
                                   f"{LABEL}. At about {facts['mass_g']:.0f} g it is more than I can hold comfortably "
                                   f"in one hand for long (about {mass_limit:.0f} g for someone like me).",
                                   min(0.9, 0.4 + (facts["mass_g"] / mass_limit - 1)),
                                   [f"estimated printed mass {facts['mass_g']:.0f} g vs about {mass_limit:.0f} g "
                                    f"comfortable for {persona.traits.hand_size} hands, {persona.traits.grip_strength} grip"])
        if check == "mass_worn" and facts.get("wearable") and facts["mass_g"] > 60:
            return PersonaReaction(pid, pattern["id"], title,
                                   f"{LABEL}. Around {facts['mass_g']:.0f} g is heavy to wear all day.",
                                   min(0.8, facts["mass_g"] / 150),
                                   [f"estimated printed mass {facts['mass_g']:.0f} g (wearables above ~60 g "
                                    "are often called heavy)"])
        if check == "grip_size" and facts.get("grip"):
            g = facts["grip"]
            lo, hi = hand["grip_diameter_comfort"]
            if pattern["id"] == "grip_too_big" and g["grip_max_mm"] > hi:
                return PersonaReaction(pid, pattern["id"], title,
                                       f"{LABEL}. At up to {g['grip_max_mm']:.0f} mm across, it's too thick for my "
                                       f"hand to wrap around (about {hi} mm is my comfortable limit).",
                                       min(0.9, 0.4 + (g["grip_max_mm"] - hi) / 20),
                                       [f"grip section {g['grip_min_mm']}-{g['grip_max_mm']} mm (equivalent "
                                        f"diameter) vs {lo}-{hi} mm comfortable for {persona.traits.hand_size} hands"])
            if pattern["id"] == "grip_too_small" and g["grip_min_mm"] < lo:
                return PersonaReaction(pid, pattern["id"], title,
                                       f"{LABEL}. At {g['grip_min_mm']:.0f} mm it's thinner than I can grip firmly.",
                                       min(0.8, 0.35 + (lo - g["grip_min_mm"]) / 20),
                                       [f"grip section {g['grip_min_mm']}-{g['grip_max_mm']} mm vs {lo}-{hi} mm "
                                        f"comfortable for {persona.traits.hand_size} hands"])
        if check == "fit_size" and facts.get("fit"):
            fit = facts["fit"]
            size = _body_size(persona)
            if size not in fit["fits_sizes"]:
                need = fit["population"][size]
                return PersonaReaction(pid, pattern["id"], title,
                                       f"{LABEL}. With an inner size of {fit['inner_mm']:.0f} mm it would not fit me "
                                       f"(my {fit['body']} is about {need} mm).", 0.8,
                                       [f"inner circumference {fit['inner_mm']:.0f} mm (+{fit['adjustable_mm']:.0f} "
                                        f"adjustable) fits sizes {fit['fits_sizes'] or 'none'}; "
                                        f"{size} {fit['body']} ~ {need} mm"])
    return None


def checklist(types: list[str]) -> list[dict[str, Any]]:
    """General patterns nobody can measure from CAD: things to check by hand, not findings."""
    return [{"type": kind, **p} for kind in types for p in knowledge().get(kind, []) if not p["facts"]]


def return_reason(reactions: list[PersonaReaction]) -> PersonaReaction | None:
    """Forced choice: the single thing most likely to make this persona stop using it."""
    return reactions[0] if reactions else None
