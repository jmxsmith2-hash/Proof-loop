"""Human-fit facts from the geometry: grip size, weight in the hand, wearable fit, openings, buttons.

Facts are measurements compared with approximate population ranges
(data/anthropometrics.yaml). Personas turn them into first-person predictions;
universal problems (a cable that cannot seat) become design findings directly.
"""

from __future__ import annotations

import math
from functools import lru_cache
from importlib import resources
from typing import Any

import numpy as np
import trimesh
import yaml

from proofloop.check.cad.checks import CadFinding, Context


@lru_cache(maxsize=1)
def anthropometrics() -> dict[str, Any]:
    return yaml.safe_load((resources.files("proofloop") / "data" / "anthropometrics.yaml").read_text(encoding="utf-8"))


def section_profile(mesh: trimesh.Trimesh, axis: int, value: float) -> dict[str, float] | None:
    """Perimeter (exact, from triangle-plane intersections) and widths of the cut at `value` along `axis`."""
    tri = mesh.triangles
    d = tri[:, :, axis] - value
    crosses = (d.min(axis=1) < 0) & (d.max(axis=1) > 0)
    if not crosses.any():
        return None
    t, dd = tri[crosses], d[crosses]
    points = []
    for i, j in ((0, 1), (1, 2), (2, 0)):
        edge = (dd[:, i] * dd[:, j]) < 0
        frac = np.where(edge, dd[:, i] / np.where(edge, dd[:, i] - dd[:, j], 1), 0)
        p = t[:, i] + (t[:, j] - t[:, i]) * frac[:, None]
        points.append(np.where(edge[:, None], p, np.nan))
    pts = np.stack(points, axis=1)                     # [n, 3 edges, 3]
    segs = []
    for row in pts:
        good = row[~np.isnan(row).any(axis=1)]
        if len(good) >= 2:
            segs.append(good[:2])
    if not segs:
        return None
    segs = np.array(segs)
    perimeter = float(np.linalg.norm(segs[:, 0] - segs[:, 1], axis=1).sum())
    plane = np.delete(segs.reshape(-1, 3), axis, axis=1)
    widths = np.ptp(plane, axis=0)
    return {"perimeter_mm": perimeter, "width_mm": float(widths.max()), "thickness_mm": float(widths.min()),
            "equivalent_diameter_mm": perimeter / math.pi}


def grip_facts(mesh: trimesh.Trimesh) -> dict[str, Any] | None:
    """Sections across the handle: along the longest axis, over the middle 60%."""
    axis = int(np.argmax(mesh.extents))
    lo, hi = mesh.bounds[0][axis], mesh.bounds[1][axis]
    profiles = []
    for f in np.linspace(0.2, 0.8, 9):
        prof = section_profile(mesh, axis, lo + f * (hi - lo))
        if prof:
            profiles.append(prof)
    if not profiles:
        return None
    eq = [p["equivalent_diameter_mm"] for p in profiles]
    return {"axis": "xyz"[axis], "length_mm": round(float(hi - lo), 1), "grip_min_mm": round(min(eq), 1),
            "grip_max_mm": round(max(eq), 1), "max_width_mm": round(max(p["width_mm"] for p in profiles), 1)}


def collect_facts(ctx: Context) -> dict[str, Any]:
    """Facts personas may use. Also adds universal findings (connector openings, tiny buttons) to ctx."""
    use, data = ctx.use, anthropometrics()
    est = ctx.metrics.get("estimates", {})
    facts: dict[str, Any] = {"mass_g": round(est.get("printed_grams", 0) + float(use.get("extra_mass_g", 0)), 1),
                             "handheld": bool(use.get("handheld")), "wearable": use.get("wearable")}
    if use.get("handheld"):
        facts["grip"] = grip_facts(ctx.mesh)
    fit = use.get("fit")
    if fit:
        body = data["body"].get(fit.get("body", "wrist"), data["body"]["wrist"])
        inner = float(fit["inner_circumference_mm"])
        adjustable = float(fit.get("adjustable_mm", 0))
        fits = [size for size, circ in body.items() if circ + 5 <= inner + adjustable and inner <= circ + 25]
        facts["fit"] = {"body": fit.get("body", "wrist"), "inner_mm": inner, "adjustable_mm": adjustable,
                        "fits_sizes": fits, "population": body}
    for port in use.get("ports", []):
        spec = data["connectors"].get(str(port.get("type", "")).lower())
        if not spec:
            continue
        w, h = float(port["width_mm"]), float(port["height_mm"])
        if w < spec["width"] or h < spec["height"]:
            ctx.add(CadFinding("connector_opening", 3, f"{port['type']} opening too small for the plug",
                               f"The {port.get('name', port['type'])} opening is {w:g} x {h:g} mm, but a typical "
                               f"{port['type']} plug's moulded body is about {spec['width']:g} x {spec['height']:g} mm, "
                               "so the cable will not plug in fully.",
                               f"Open it to at least {spec['width'] + 0.5:g} x {spec['height'] + 0.5:g} mm (or "
                               "recess the port).", None, {"opening": [w, h], "plug": [spec["width"], spec["height"]]}))
    buttons = [float(b) for b in use.get("buttons_mm", [])]
    if buttons:
        facts["smallest_button_mm"] = min(buttons)
        if min(buttons) < data["fingertip_min_button_mm"]:
            ctx.add(CadFinding("button_size", 2, "Buttons smaller than a fingertip",
                               f"The smallest button is {min(buttons):g} mm; about {data['fingertip_min_button_mm']} mm "
                               "is the comfortable minimum for a fingertip.", "Make buttons at least 10 mm across.",
                               None, {"smallest_mm": min(buttons)}))
    facts["findings"] = {}
    for f in sorted(ctx.findings, key=lambda f: f.severity):  # most severe finding per check wins
        facts["findings"][f.check] = f
    return facts
