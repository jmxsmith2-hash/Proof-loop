"""Design iterations: compare versions, learn from real prints, and feed that into the next check."""

from __future__ import annotations

import csv
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np

from synthux.core.db import Database, utcnow

# What a printed defect means, what to do, and how it should change future checks for this printer+material.
DEFECTS: dict[str, dict[str, Any]] = {
    "warping": {"advice": "Corners lifted: use a brim, a warmer bed or an enclosure, and round sharp corners.",
                "adjust": {"warp_risk": "high"}},
    "poor_bed_adhesion": {"advice": "First layer let go: clean the bed, slow the first layer, add a brim.",
                          "adjust": {}},
    "stringing": {"advice": "Thin strings between parts: raise retraction or lower the nozzle temperature.",
                  "adjust": {}},
    "elephant_foot": {"advice": "Bottom layers bulge: add a 0.4 mm chamfer at the base or enable first-layer "
                                "compensation.", "adjust": {}},
    "poor_overhang": {"advice": "Overhangs drooped: this printer needs a gentler angle; supports or chamfers.",
                      "adjust": {"max_overhang_deg": -5}},
    "bridge_sag": {"advice": "Bridges sagged: shorten unsupported spans or add supports.",
                   "adjust": {"max_bridge_mm": -5}},
    "broke": {"advice": "A part broke: thicken it, add fillets, or rotate so layers run along the load.",
              "adjust": {"min_wall_mm": 0.4}},
    "too_tight": {"advice": "Parts did not fit: open clearances for this printer.", "adjust": {"clearance_mm": 0.1}},
    "too_loose": {"advice": "Parts rattle: reduce clearances slightly.", "adjust": {"clearance_mm": -0.05}},
    "layer_shift": {"advice": "Layers shifted: a mechanical or speed problem, not the design (check belts).",
                    "adjust": {}},
    "supports_hard_to_remove": {"advice": "Supports were hard to remove: reorient to need fewer, or use a "
                                          "support interface gap.", "adjust": {}},
}
TUNABLE = ("max_overhang_deg", "max_bridge_mm", "min_wall_mm", "clearance_mm")


# ------------------------------------------------------------------ versions


def _close(a: dict | None, b: dict | None, tol: float) -> bool:
    if not a or not b:
        return a is None and b is None
    return float(np.linalg.norm([a["x"] - b["x"], a["y"] - b["y"], a["z"] - b["z"]])) <= tol


def compare_versions(prev: list[dict[str, Any]], new: list[dict[str, Any]], extent: float) -> dict[str, list]:
    """Match findings by check type and location (within 15% of the part size or 15 mm)."""
    tol = max(15.0, 0.15 * extent)
    remaining = list(range(len(prev)))
    persisting, new_only = [], []
    for f in new:
        match = next((i for i in remaining if prev[i]["check"] == f["check"] and _close(prev[i]["location"],
                                                                                        f["location"], tol)), None)
        if match is None:
            new_only.append(f)
        else:
            remaining.remove(match)
            persisting.append({**f, "was_severity": prev[match]["severity"]})
    return {"fixed": [prev[i] for i in remaining], "persisting": persisting, "new": new_only}


def metric_changes(prev: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, label in (("printed_grams", "printed mass (g)"), ("print_hours", "print time (h)"),
                       ("material_cost", "material cost")):
        a, b = prev.get("estimates", {}).get(key), new.get("estimates", {}).get(key)
        if a is not None and b is not None:
            rows.append({"metric": label, "before": a, "after": b, "change": round(b - a, 2)})
    for key, label in (("overhang_area_mm2", "overhang area (mm²)"), ("tip_angle_deg", "tip angle (°)"),
                       ("bed_contact_mm2", "bed contact (mm²)")):
        a, b = prev.get(key), new.get(key)
        if a is not None and b is not None:
            rows.append({"metric": label, "before": a, "after": b, "change": round(b - a, 2)})
    return rows


# -------------------------------------------------------------------- prints


def read_measurements(path: Path) -> list[dict[str, Any]]:
    """CSV: feature, designed_mm, measured_mm[, kind (outer|hole|gap)][, axis (x|y|z)]."""
    rows = []
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if not row.get("feature"):
                continue
            rows.append({"feature": row["feature"].strip(), "designed_mm": float(row["designed_mm"]),
                         "measured_mm": float(row["measured_mm"]),
                         "kind": (row.get("kind") or "outer").strip().lower(),
                         "axis": (row.get("axis") or "").strip().lower()})
    if not rows:
        raise ValueError(f"{path}: no measurements (columns: feature, designed_mm, measured_mm, kind, axis)")
    return rows


def analyse_print(measurements: list[dict[str, Any]], defects: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"deviations": [], "advice": [], "adjust": {}}
    outer_ratio: dict[str, list[float]] = {"x": [], "y": [], "z": [], "": []}
    hole_offsets, gap_offsets = [], []
    for m in measurements:
        diff = m["measured_mm"] - m["designed_mm"]
        out["deviations"].append({**m, "diff_mm": round(diff, 3),
                                  "pct": round(100 * diff / m["designed_mm"], 2) if m["designed_mm"] else None})
        if m["kind"] == "hole":
            hole_offsets.append(diff)
        elif m["kind"] == "gap":
            gap_offsets.append(diff)
        elif m["designed_mm"] > 0:
            outer_ratio[m["axis"] if m["axis"] in outer_ratio else ""].append(m["measured_mm"] / m["designed_mm"])
    scale = {}
    for axis, ratios in outer_ratio.items():
        if ratios and axis:
            scale[axis] = round(median(ratios), 4)
    all_outer = [r for rs in outer_ratio.values() for r in rs]
    if all_outer:
        scale.setdefault("all", round(median(all_outer), 4))
    out["scale"] = scale
    for axis, value in scale.items():
        if axis == "all" and len(scale) > 1:
            continue  # per-axis advice already covers it
        if abs(value - 1) >= 0.004:
            out["advice"].append(f"Outer dimensions{'' if axis == 'all' else ' in ' + axis.upper()} came out "
                                 f"{(value - 1) * 100:+.1f}%: scale by {1 / value:.4f} in the slicer to compensate.")
    if hole_offsets:
        h = round(median(hole_offsets), 3)
        out["hole_offset_mm"] = h
        if h < -0.05:
            out["advice"].append(f"Holes printed {abs(h):.2f} mm small: design holes {abs(h):.2f} mm larger "
                                 "(or ream them).")
    if gap_offsets:
        g = round(median(gap_offsets), 3)
        out["gap_offset_mm"] = g
        if g < -0.03:
            out["advice"].append(f"Gaps closed up by {abs(g):.2f} mm: add that to every clearance.")
            out["adjust"]["clearance_mm"] = round(abs(g), 3)
    for d in defects:
        info = DEFECTS.get(d)
        if info is None:
            out["advice"].append(f"Recorded defect '{d}' (no specific advice).")
            continue
        out["advice"].append(info["advice"])
        for key, delta in info["adjust"].items():
            if key == "clearance_mm" and "gap_offset_mm" in out:
                continue  # measured gaps already set the clearance; don't count the same evidence twice
            if isinstance(delta, (int, float)):
                out["adjust"][key] = round(out["adjust"].get(key, 0) + delta, 3)
            else:
                out["adjust"][key] = delta
    return out


def calibration_key(printer: str, material: str) -> str:
    return f"{printer}|{material}"


def learned(db: Database, printer: str, material: str) -> dict[str, Any]:
    row = db.one("SELECT data_json FROM printer_calibration WHERE key = ?", [calibration_key(printer, material)])
    return row["data_json"] if row else {}


def update_learned(db: Database, printer: str, material: str, analysis: dict[str, Any]) -> dict[str, Any]:
    """Accumulate what real prints taught us about this printer + material."""
    data = learned(db, printer, material) or {"prints": 0, "adjust": {}, "scale": {}, "hole_offset_mm": None}
    data["prints"] += 1
    for key, delta in analysis.get("adjust", {}).items():
        if isinstance(delta, (int, float)):
            data["adjust"][key] = round(data["adjust"].get(key, 0) + delta, 3)
        else:
            data["adjust"][key] = delta
    for axis, value in analysis.get("scale", {}).items():
        prev = data["scale"].get(axis)
        data["scale"][axis] = round(value if prev is None else (prev + value) / 2, 4)
    if analysis.get("hole_offset_mm") is not None:
        prev = data.get("hole_offset_mm")
        data["hole_offset_mm"] = analysis["hole_offset_mm"] if prev is None else round((prev + analysis["hole_offset_mm"]) / 2, 3)
    db.insert("printer_calibration", {"key": calibration_key(printer, material), "updated_at": utcnow(),
                                      "data_json": data}, replace=True)
    return data


def apply_learned(material: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Material profile with what real prints taught us applied on top."""
    out = dict(material)
    for key, value in (data or {}).get("adjust", {}).items():
        if key in TUNABLE and isinstance(value, (int, float)):
            out[key] = round(float(out[key]) + float(value), 3)
        elif key == "warp_risk":
            out[key] = value
    if data and data.get("hole_offset_mm") is not None:
        out["hole_offset_mm"] = float(data["hole_offset_mm"])   # measured: negative = holes print small
    if data:
        out["learned_from_prints"] = data.get("prints", 0)
    return out
