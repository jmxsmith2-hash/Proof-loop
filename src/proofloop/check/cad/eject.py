"""Auto-eject readiness: will FarmLoop's pusher clear this part off the plate (ProofLoop's sweep)?

A loop file pushes each part at its own height minus a push depth (FarmLoop uses 10 mm), never below
the pusher's calibrated floor, moving from the back of the plate (Y 250) to the front, in lanes
from X 30 to X 220 on a P2S. The bed first cools to a release temperature, and on Stage 2 the
FarmBoard bends the plate. These numbers are read from your loop-file settings
(``settings.json`` in ProofLoop's folder), otherwise the defaults for new loop files are used.

Per part standing on the bed:

* too low to push  - not above the sweep floor: the pusher passes over it;
* barely above it  - pushed at its very top edge, it can be skipped or ridden over;
* tips over        - pushed high on a short footprint, it falls rather than slides (fine if it falls clear);
* sticks           - a large flat contact patch may not release at the release temperature;
* tiny             - small, light parts slip under the pusher or bounce back;

and how many copies fit on one plate inside the lanes, away from the sweep's start line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from proofloop.check.cad.checks import CadFinding, _loc
from proofloop.core.paths import home


def _finding(kind: str, severity: int, title: str, detail: str, fix: str, where=None, data=None) -> CadFinding:
    """Each kind gets its own fingerprint: several can apply to one part at one place."""
    spot = f"{round(where['x'])}:{round(where['y'])}" if where else "plate"
    return CadFinding("auto_eject", severity, title, detail, fix, where, data or {},
                      fingerprint=f"auto_eject.{kind}@{spot}")

P2S_SWEEP = {"lanes_x": [30.0, 70.0, 100.0, 130.0, 160.0, 190.0, 220.0], "reach": 15.0, "y_rear": 250.0,
             "rear_clearance": 10.0, "plate": [256.0, 256.0]}
FRICTION = 0.4        # textured PEI after cooling, roughly; tipping vs sliding
STICKY_CM2 = 80.0     # contact patches this large often need the bend (or a cooler release) to let go
GAP_MM = 5.0          # between copies on the plate


def eject_settings(data_dir: Path | str | None = None) -> dict[str, Any]:
    out = {"floor_z": 2.0, "floor_calibrated": False, "push_offset": 10.0, "bender_fitted": True,
           "release_temp": 30.0, "from_settings": False, **P2S_SWEEP}
    path = Path(data_dir or home()) / "settings.json"
    try:
        defaults = json.loads(path.read_text(encoding="utf-8")).get("defaults") or {}
    except (OSError, ValueError):
        return out
    for key in ("floor_z", "floor_calibrated", "push_offset", "bender_fitted", "release_temp"):
        if key in defaults:
            out[key] = defaults[key]
    out["from_settings"] = True
    return out


def _parts(ctx) -> list[Any]:
    """Bodies that stand on the bed (each is pushed on its own)."""
    mesh = ctx.mesh
    bed = float(mesh.bounds[0, 2])
    shells = ctx.model.shells() if hasattr(ctx.model, "shells") else [mesh]
    on_bed = [s for s in shells if float(s.bounds[0, 2]) - bed < 0.3]
    return on_bed or [mesh]


def _contact_cm2(body, bed: float) -> float:
    down = (body.face_normals[:, 2] < -0.99) & (body.triangles_center[:, 2] - bed < 0.05)
    return float(body.area_faces[down].sum()) / 100.0


def check_auto_eject(ctx) -> None:
    if not ctx.use.get("auto_eject"):
        return
    st = eject_settings(ctx.use.get("data_dir"))
    floor, offset = float(st["floor_z"]), float(st["push_offset"])
    bed = float(ctx.mesh.bounds[0, 2])
    parts = []
    for body in _parts(ctx):
        (x0, y0, z0), (x1, y1, z1) = body.bounds
        h, w, d = z1 - bed, x1 - x0, y1 - y0
        push = max(floor, h - offset)
        contact = _contact_cm2(body, bed)
        centre = body.bounds.mean(axis=0)
        parts.append({"height": round(h, 1), "push_z": round(push, 1), "width": round(w, 1), "depth": round(d, 1),
                      "contact_cm2": round(contact, 1)})
        where = _loc(np.array([centre[0], centre[1], bed + h / 2]), max(w, d) / 2)
        floor_note = "" if st["floor_calibrated"] else " (the default floor: calibrate it under Loop files › Calibrate)"
        if h <= floor + 0.3:
            ctx.add(_finding("low", 4, "Too low for the pusher",
                               f"The part is {h:.1f} mm tall; the pusher never goes below {floor:g} mm{floor_note}, so "
                               "it passes over the part and leaves it on the plate.",
                               f"Make it at least {floor + 2:g} mm tall (or add a sacrificial tab to push), or clear "
                               "these by hand.", where, {"height_mm": round(h, 2), "floor_mm": floor}))
            continue
        if h <= floor + 1.0:
            ctx.add(_finding("barely", 2, "Barely above the pusher's floor",
                               f"The part is {h:.1f} mm tall and is pushed at {push:.1f} mm, right at its top edge; "
                               "the pusher can ride over it, especially if the plate isn't flat.",
                               f"Make it {floor + 2:g} mm tall or more, or calibrate the floor lower if your pusher "
                               "allows.", where, {"height_mm": round(h, 2), "push_z": round(push, 2)}))
        if push > d / (2 * FRICTION) and d > 0:
            ctx.add(_finding("tips", 1, "Tips over when pushed",
                               f"Pushed at {push:.0f} mm on a footprint only {d:.0f} mm deep (front to back), it tips "
                               "forward rather than sliding. That's fine if it falls clear of the plate.",
                               "Turn it so its long side faces the front, or push it lower in Make a loop file (part slider).",
                               where, {"push_z": round(push, 1), "depth_mm": round(d, 1)}))
        if contact > STICKY_CM2:
            bender = st["bender_fitted"]
            ctx.add(_finding("sticks", 1 if bender else 2, "Large flat contact may not let go",
                               f"About {contact:.0f} cm² of the part sits flat on the plate. Large patches can stay "
                               f"stuck at the {st['release_temp']:g} °C release temperature"
                               + ("; the bend usually frees them." if bender else ", and no bender is fitted."),
                               "Lower the release temperature (25 °C), add chamfered or raised feet, or fit the "
                               "FarmBoard bender.", where, {"contact_cm2": round(contact, 1)}))
        if max(w, d) < 6 and h < 6:
            ctx.add(_finding("tiny", 2, "Very small part",
                               f"At {w:.0f} x {d:.0f} x {h:.0f} mm it is light enough to slip under the pusher or "
                               "bounce back onto the plate.", "Print small parts on a sprue or a shared base, or in "
                               "a tray that is pushed as one.", where, {"size_mm": [round(w, 1), round(d, 1), round(h, 1)]}))
    # copies per plate: inside the lanes' reach, away from the sweep's start line at the back
    (mx0, my0, _), (mx1, my1, _) = ctx.mesh.bounds
    w, d = mx1 - mx0, my1 - my0
    lanes = st["lanes_x"]
    x_span = (max(lanes) + st["reach"]) - (min(lanes) - st["reach"])
    y_span = st["y_rear"] - st["rear_clearance"]
    across = int((x_span + GAP_MM) // (w + GAP_MM)) if w > 0 else 0
    deep = int((y_span + GAP_MM) // (d + GAP_MM)) if d > 0 else 0
    ctx.metrics["auto_eject"] = {"settings": {k: st[k] for k in ("floor_z", "floor_calibrated", "push_offset",
                                                                  "bender_fitted", "release_temp", "from_settings")},
                                 "parts": parts, "copies_per_plate": across * deep, "layout": [across, deep],
                                 "usable_x": [min(lanes) - st["reach"], max(lanes) + st["reach"]],
                                 "rear_limit_y": y_span}
    if across * deep == 0:
        ctx.add(_finding("lanes", 3, "Doesn't fit inside the sweep's lanes",
                           f"The part is {w:.0f} x {d:.0f} mm; the pusher's lanes reach X {min(lanes) - st['reach']:g} to "
                           f"{max(lanes) + st['reach']:g} mm and Y up to {y_span:g} mm.",
                           "Make it smaller, or turn it on the plate."))


def copies_note(metrics: dict[str, Any], grams: float | None) -> str | None:
    ae = metrics.get("auto_eject")
    if not ae or not ae["copies_per_plate"]:
        return None
    n = ae["copies_per_plate"]
    g = f", about {grams * n:.0f} g per loop" if grams else ""
    return f"{n} copies fit on one plate ({ae['layout'][0]} across x {ae['layout'][1]} deep){g}"


