"""Checks on sliced G-code: will this file print well on this printer, with this material?

Each check measures the file itself (every move, temperature and fan command) against the printer
profile and the material's usual ranges from config/printers.yaml:

* bounds        - extrusion outside the build plate or above the build height, or below the bed
* printer       - sliced for another printer or nozzle than the one you chose
* first_layer   - first-layer height, speed and bed temperature
* layer_height  - layers too thick for the nozzle (weak bonding) or extremely thin
* temperature   - nozzle and bed against the material and the printer's limits; extruding cold
* flow          - melt rate (mm³/s) the moves ask for against the hot end and the material
* extrusion     - line widths that imply the wrong filament diameter or flow
* retraction    - retraction length for the extruder type; long travels without retraction
* cooling       - part fan against what the material wants; layers too short to cool
* adhesion      - warping materials on open printers, tall narrow parts without a brim
* end           - heaters left on at the end
* consistency   - the file against its own header (edited after slicing? another plate?)

Speeds are the ones commanded; the printer may go slower on short moves and corners,
so flow figures are upper bounds for short moves and close to reality on long ones.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from synthux.physical.cad.checks import CadFinding
from synthux.physical.gcode.parse import _spread

DEFAULT_ACCEL = 5000.0
COLD_EXTRUSION_C = 170.0         # Marlin/Bambu refuse to extrude below about this


@dataclass
class GcodeContext:
    trace: Any
    printer: dict[str, Any]
    material: dict[str, Any]
    nozzle: float
    filament_diameter: float
    findings: list[CadFinding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    # derived columns
    xy: np.ndarray = field(default=None, repr=False)
    model: np.ndarray = field(default=None, repr=False)
    extrude: np.ndarray = field(default=None, repr=False)
    travel: np.ndarray = field(default=None, repr=False)
    time: np.ndarray = field(default=None, repr=False)
    layer_z: np.ndarray = field(default=None, repr=False)
    layer_h: np.ndarray = field(default=None, repr=False)
    layer_loop: np.ndarray = field(default=None, repr=False)
    move_h: np.ndarray = field(default=None, repr=False)
    flow: np.ndarray = field(default=None, repr=False)
    width: np.ndarray = field(default=None, repr=False)
    images: dict[str, str] = field(default_factory=dict)
    cache: dict[Any, Any] = field(default_factory=dict, repr=False)
    history: dict[str, Any] | None = None     # what real prints on this printer + material taught

    @property
    def label(self) -> str:
        return (self.printer.get("names") or [self.printer["name"]])[0]

    @property
    def fil_area(self) -> float:
        return math.pi * (self.filament_diameter / 2) ** 2

    def add(self, check: str, severity: int, title: str, detail: str, fix: str = "", *, layer: int | None = None,
            at: tuple[float, float, float] | None = None, image: str | None = None, **data: Any) -> None:
        loc = {"x": round(at[0], 1), "y": round(at[1], 1), "z": round(at[2], 2)} if at else None
        if layer is not None:
            data["layer"] = int(layer) + 1                       # across the whole file (pictures use it)
            if self.metrics.get("loops", 1) > 1 and self.layer_loop is not None and 0 <= layer < len(self.layer_loop):
                lp = self.layer_loop[layer]
                order = sorted(set(self.layer_loop[~np.isnan(self.layer_h)].tolist()))
                firsts = np.flatnonzero((self.layer_loop == lp) & ~np.isnan(self.layer_h))
                data["loop_no"] = order.index(int(lp)) + 1 if int(lp) in order else None
                data["layer_in_loop"] = int(layer - firsts[0]) + 1 if len(firsts) else None
        if image:
            data["image_key"] = image
        self.findings.append(CadFinding(check, severity, title, detail, fix, loc, data, fingerprint=check))


# ----------------------------------------------------------------------------------------- preparation

def prepare(ctx: GcodeContext) -> None:
    t = ctx.trace
    ctx.xy = np.hypot(t.x1 - t.x0, t.y1 - t.y0)
    ctx.extrude = (t.de > 1e-7) & (ctx.xy > 1e-6)
    ctx.travel = (t.de <= 1e-7) & (ctx.xy > 1e-6)
    # Bambu files mark where the print starts and ends: start code (purge) and end code (park, a loop
    # file's ejection sweep) are left out of the print checks.
    printing = (t.phase == 1) if (t.phase is not None and t.phase_marked) else None
    if t.layer_markers:
        ctx.model = t.layer >= 0
        if printing is not None:
            ctx.model &= printing
        layer = t.layer.copy()
    else:   # no markers: every rise in extrusion height is a new layer; each loop starts on the bed again
        ctx.model = printing if printing is not None else np.ones(len(t), bool)
        zs = np.round(t.z, 3)
        layer = np.zeros(len(t), np.int32)
        base = 0
        loops = t.loop if t.loop is not None else np.zeros(len(t), np.int32)
        for lp in np.unique(loops):
            idx = np.flatnonzero(loops == lp)
            ext = ctx.extrude[idx] & ctx.model[idx]
            cur = np.fmax.accumulate(np.where(ext, zs[idx], -1.0))
            uniq = np.unique(cur[ext]) if ext.any() else np.zeros(0)
            layer[idx] = np.searchsorted(uniq, cur) + base
            base += len(uniq)
        t.layer = layer
    n_layers = int(layer.max()) + 1 if len(layer) and layer.max() >= 0 else 0
    sel = ctx.extrude & ctx.model
    lz = np.full(n_layers, np.nan)
    layer_loop = np.zeros(n_layers, np.int32)
    if sel.any():
        idx = layer[sel]
        if t.loop is not None:
            layer_loop[idx] = t.loop[sel]
        order = np.argsort(idx, kind="stable")
        zs, idx = t.z[sel][order], idx[order]
        starts = np.searchsorted(idx, np.arange(n_layers))
        ends = np.searchsorted(idx, np.arange(n_layers), side="right")
        for i in range(n_layers):
            if ends[i] > starts[i]:
                lz[i] = float(np.median(zs[starts[i]:ends[i]]))
    keep = ~np.isnan(lz)
    ctx.layer_z = lz
    ctx.layer_loop = layer_loop
    heights = np.full(n_layers, np.nan)
    prev, prev_loop = 0.0, None
    for i in np.flatnonzero(keep):
        if layer_loop[i] != prev_loop:          # a new loop prints its first layer on the bed again
            prev, prev_loop = 0.0, layer_loop[i]
        heights[i] = lz[i] - prev
        prev = lz[i]
    ctx.layer_h = heights
    ctx.metrics["loops"] = max(1, len(np.unique(layer_loop[keep]))) if keep.any() else 1
    li = np.clip(layer, 0, max(0, n_layers - 1))
    ctx.move_h = heights[li] if n_layers else np.full(len(t), 0.2)
    ctx.move_h = np.where(np.isnan(ctx.move_h) | (ctx.move_h <= 0), ctx.nozzle / 2, ctx.move_h)

    v = np.maximum(t.feed, 0.1)
    a = np.where(t.accel > 0, t.accel, float(ctx.printer.get("accel_mm_s2", DEFAULT_ACCEL)))
    naive = t.length / v
    full = t.length >= v * v / a
    stop = np.where(full, t.length / v + v / a, 2 * np.sqrt(t.length / a))
    ctx.time = (naive + stop) / 2          # between "corners at full speed" and "stop at every corner"
    vol = np.where(ctx.extrude, t.de * ctx.fil_area, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        ctx.flow = np.where(ctx.extrude, vol * v / ctx.xy, 0.0)
        ctx.width = np.where(ctx.extrude, vol / (ctx.xy * ctx.move_h), 0.0)


def _at_moves(events: list[tuple], n: int, default: float = np.nan) -> np.ndarray:
    """The value set by the last event before each move (events apply to the moves after them)."""
    return _spread(events, n, default)


def _where(ctx: GcodeContext, i: int) -> tuple[float, float, float]:
    t = ctx.trace
    return float(t.x1[i]), float(t.y1[i]), float(t.z[i])


# ----------------------------------------------------------------------------------------- checks

def check_bounds(ctx: GcodeContext) -> None:
    t = ctx.trace
    sel = ctx.extrude & ctx.model
    if not sel.any():
        ctx.add("bounds", 4, "Nothing is printed", "The file has no extruding moves after the start code.",
                "Re-slice the model; the file may be only start and end code.")
        return
    bx, by, bz = (float(v) for v in ctx.printer["build_volume"])
    margin = 0.5
    xs = np.concatenate([t.x0[sel], t.x1[sel]])
    ys = np.concatenate([t.y0[sel], t.y1[sel]])
    ctx.metrics["extents"] = {"x": [round(float(xs.min()), 1), round(float(xs.max()), 1)],
                              "y": [round(float(ys.min()), 1), round(float(ys.max()), 1)],
                              "z": round(float(t.z[sel].max()), 2)}
    outside = sel & ((np.minimum(t.x0, t.x1) < -margin) | (np.maximum(t.x0, t.x1) > bx + margin) |
                     (np.minimum(t.y0, t.y1) < -margin) | (np.maximum(t.y0, t.y1) > by + margin))
    if outside.any():
        i = int(np.flatnonzero(outside)[0])
        over = max(-xs.min(), xs.max() - bx, -ys.min(), ys.max() - by)
        ctx.add("bounds", 4, "Prints outside the build plate",
                f"{int(outside.sum())} extruding moves go up to {over:.1f} mm past the {bx:g} x {by:g} mm plate of the "
                f"{ctx.label} (the part spans X {xs.min():.1f}..{xs.max():.1f}, Y {ys.min():.1f}..{ys.max():.1f}).",
                "Move the part onto the plate in the slicer (Arrange) and re-slice, or check the printer chosen "
                "when slicing.", layer=int(t.layer[i]), at=_where(ctx, i), image="plate")
    top = float(t.z[sel].max())
    if top > bz + margin:
        i = int(np.argmax(np.where(sel, t.z, -1)))
        ctx.add("bounds", 4, "Taller than the printer can print",
                f"Extrusion reaches Z {top:.1f} mm; the {ctx.label} prints up to {bz:g} mm.",
                "Lay the part down or split it.", layer=int(t.layer[i]), at=_where(ctx, i))
    low = sel & (t.z < 0.04)
    if low.any():
        i = int(np.flatnonzero(low)[0])
        ctx.add("bounds", 4, "Extrudes at or below the bed",
                f"{int(low.sum())} extruding moves are at Z {t.z[i]:.2f} mm: the nozzle would scrape the plate.",
                "Check the Z offset and the first-layer height in the slicer.", layer=int(t.layer[i]), at=_where(ctx, i))


def check_printer(ctx: GcodeContext, header_printer: str | None, header_nozzle: float | None) -> None:
    names = [n.lower() for n in ctx.printer.get("names", [])]
    if header_printer and names and not any(n == header_printer.lower() for n in names):
        ctx.add("printer", 3, "Sliced for a different printer",
                f"The file says it was sliced for \"{header_printer}\", but you are checking it for the "
                f"{ctx.label}. Its start code, bed size and speed limits may not match.",
                f"Re-slice with the {ctx.label} profile, or pass --printer for the printer you will use.")
    if header_nozzle and abs(header_nozzle - float(ctx.printer["nozzle"])) > 0.01:
        ctx.add("printer", 2, f"Sliced for a {header_nozzle:g} mm nozzle",
                f"The profile assumes {ctx.printer['nozzle']:g} mm; the file was sliced for {header_nozzle:g} mm. "
                "The checks below use the file's nozzle size.",
                f"Fit the {header_nozzle:g} mm nozzle (and set it on the printer) before printing.")


def check_layers(ctx: GcodeContext) -> None:
    t, h = ctx.trace, ctx.layer_h
    valid = np.flatnonzero(~np.isnan(h))
    if not len(valid):
        return
    first = float(h[valid[0]])
    ctx.metrics["first_layer_mm"] = round(first, 3)
    noz = ctx.nozzle
    if first > 0.85 * noz:
        ctx.add("first_layer", 3, "First layer too thick for the nozzle",
                f"The first layer is {first:.2f} mm with a {noz:g} mm nozzle; above about {0.85 * noz:.2f} mm it is "
                "not squashed onto the plate and tends to come loose.",
                f"Use a first layer of {min(0.3, 0.6 * noz):.2f}-{0.75 * noz:.2f} mm.", layer=int(valid[0]), image="layer1")
    elif first < 0.1:
        ctx.add("first_layer", 2, "Very thin first layer",
                f"The first layer is {first:.2f} mm; any unevenness in the plate shows up as gaps or scraping.",
                "Use 0.2 mm or more unless the plate is perfectly level.", layer=int(valid[0]))
    first_sel = ctx.extrude & (t.layer == valid[0])
    if first_sel.sum() > 5:
        speed = float(np.median(t.feed[first_sel]))
        ctx.metrics["first_layer_speed_mm_s"] = round(speed, 0)
        if speed > 150:
            ctx.add("first_layer", 2, "Fast first layer",
                    f"Most first-layer lines run at about {speed:.0f} mm/s; first layers stick best slower.",
                    "Set the initial layer speed to about 50 mm/s.", layer=int(valid[0]))
    rest = valid[1:]
    if len(rest):
        thick = rest[h[rest] > 0.8 * noz + 1e-6]
        if len(thick):
            ctx.add("layer_height", 3, "Layers too thick for the nozzle",
                    f"{len(thick)} layer(s) are up to {h[thick].max():.2f} mm high with a {noz:g} mm nozzle (from layer "
                    f"{thick[0] + 1}); above about {0.8 * noz:.2f} mm layers bond poorly and the part splits along them.",
                    f"Keep layers at or below {0.75 * noz:.2f} mm, or fit a larger nozzle.", layer=int(thick[0]),
                    image="side")
        thin = rest[h[rest] < 0.04]
        if len(thin) > 3:
            ctx.add("layer_height", 1, "Extremely thin layers",
                    f"{len(thin)} layers are under 0.04 mm; they add a lot of time for little visible gain.",
                    "Check the layer height (or variable layer height) settings.", layer=int(thin[0]))
    loops = ctx.metrics.get("loops", 1)
    ctx.metrics["layers_total"] = int(len(valid))
    ctx.metrics["layers"] = int((ctx.layer_loop[valid] == ctx.layer_loop[valid[0]]).sum()) if loops > 1 \
        else int(len(valid))                                     # layers of one loop (the first plate)
    ctx.metrics["layer_heights_mm"] = [float(v) for v in np.unique(np.round(h[valid], 2))[:8]]


def check_temperatures(ctx: GcodeContext) -> None:
    t, mat, pr = ctx.trace, ctx.material, ctx.printer
    n = len(t)
    noz = _at_moves([(i, v) for i, v, _ in t.nozzle_events], n)
    bed = _at_moves(t.bed_events, n)
    sel = ctx.extrude & ctx.model
    if not sel.any():
        return
    used = noz[sel]
    known = used[~np.isnan(used)]
    lo, hi = mat.get("nozzle_c", [None, None])
    if len(known):
        tmin, tmax = float(known.min()), float(known.max())
        ctx.metrics["nozzle_c"] = [int(v) for v in np.unique(np.round(known))[:6]]
        if pr.get("max_nozzle_temp") and tmax > float(pr["max_nozzle_temp"]):
            ctx.add("temperature", 4, "Nozzle hotter than the printer allows",
                    f"The file sets {tmax:.0f}°C; the {pr['name']} goes up to {pr['max_nozzle_temp']}°C.",
                    "Lower the nozzle temperature, or use a printer with an all-metal high-temperature hot end.")
        cold = sel & (noz < COLD_EXTRUSION_C)
        if cold.any():
            i = int(np.flatnonzero(cold)[0])
            ctx.add("temperature", 4, "Extrudes with the nozzle cold",
                    f"Extrusion starts at layer {int(t.layer[i]) + 1} while the nozzle is set to {noz[i]:.0f}°C; "
                    "the printer stops cold extrusion, or the filament grinds.",
                    "Make sure the start code heats the nozzle (M109) before printing.", layer=int(t.layer[i]))
        elif lo is not None and tmin < lo - 10:
            ctx.add("temperature", 3, f"Nozzle too cold for {mat['name']}",
                    f"Printing at {tmin:.0f}°C; {mat['name']} usually prints at {lo}-{hi}°C. Too cold gives weak layers, "
                    "under-extrusion and clogs.", f"Print at {lo}-{hi}°C (check the filament's label).")
        elif lo is not None and tmin < lo:
            ctx.add("temperature", 2, f"Nozzle on the cold side for {mat['name']}",
                    f"Printing at {tmin:.0f}°C; {mat['name']} usually prints at {lo}-{hi}°C.",
                    "Fine for slow prints; raise it a little if layers split or lines look thin.")
        if hi is not None and tmax > hi + 10:
            ctx.add("temperature", 3, f"Nozzle too hot for {mat['name']}",
                    f"Printing at {tmax:.0f}°C; {mat['name']} usually prints at {lo}-{hi}°C. Too hot gives stringing, "
                    "oozing, discolouring and (for PLA) heat creep jams.", f"Print at {lo}-{hi}°C.")
        elif hi is not None and tmax > hi:
            ctx.add("temperature", 2, f"Nozzle on the hot side for {mat['name']}",
                    f"Printing at {tmax:.0f}°C; {mat['name']} usually prints at {lo}-{hi}°C.",
                    "Fine for very fast prints; lower it if you see stringing.")
        first = int(np.flatnonzero(sel)[0])
        waits = [i for i, v, w in t.nozzle_events if w and i <= first and v >= (lo or COLD_EXTRUSION_C) - 15]
        if not waits and not (np.nan_to_num(noz[first]) < COLD_EXTRUSION_C):
            ctx.add("temperature", 2, "Does not wait for the nozzle to heat",
                    "There is no M109 (heat and wait) before printing starts, so the first lines may go down "
                    "before the nozzle is up to temperature.", "Use M109 in the start code, or preheat by hand.")
    bused = bed[sel]
    bknown = bused[~np.isnan(bused)]
    blo, bhi = mat.get("bed_c", [None, None])
    if len(bknown):
        bmin, bmax = float(bknown.min()), float(bknown.max())
        ctx.metrics["bed_c"] = [int(v) for v in np.unique(np.round(bknown))[:4]]
        if pr.get("max_bed_temp") and bmax > float(pr["max_bed_temp"]) + 0.5:
            ctx.add("temperature", 4, "Bed hotter than the printer allows",
                    f"The file sets the bed to {bmax:.0f}°C; the {pr['name']} goes up to {pr['max_bed_temp']}°C.",
                    "Lower the bed temperature.")
        if blo is not None and bmin < blo - 5:
            warp = mat.get("warp_risk") == "high"
            ctx.add("temperature", 3 if warp else 2, f"Bed too cool for {mat['name']}",
                    f"The bed is at {bmin:.0f}°C; {mat['name']} usually wants {blo}-{bhi}°C. "
                    + ("It will likely warp and lift at the corners." if warp else "The first layer may not stick."),
                    f"Set the bed to {blo}-{bhi}°C.", layer=0)
        elif bhi is not None and bmax > bhi + 10:
            ctx.add("temperature", 2, f"Bed hotter than {mat['name']} needs",
                    f"The bed is at {bmax:.0f}°C; {mat['name']} usually wants {blo}-{bhi}°C. Too hot softens the bottom "
                    "layers (elephant's foot) and, for PLA, can cause heat creep in an enclosed printer.",
                    f"Set the bed to {blo}-{bhi}°C.")
    elif mat.get("warp_risk") == "high":
        ctx.add("temperature", 2, "Bed temperature is never set",
                f"{mat['name']} needs a hot bed ({blo}-{bhi}°C) and the file never sets one.",
                f"Set the bed to {blo}-{bhi}°C in the slicer's filament profile.")


def check_flow(ctx: GcodeContext) -> None:
    t = ctx.trace
    sel = ctx.extrude & ctx.model & (ctx.xy >= 1.0)
    if sel.sum() < 5:
        return
    flow, tm = ctx.flow[sel], ctx.time[sel]
    hot = float(ctx.printer.get("max_flow_mm3_s") or 0) or None
    mat_max = float(ctx.material.get("flow_mm3_s") or 0) or None
    peak_i = int(np.flatnonzero(sel)[int(np.argmax(flow))])
    peak = float(flow.max())
    ctx.metrics["peak_flow_mm3_s"] = round(peak, 1)
    ctx.metrics["typical_flow_mm3_s"] = round(float(np.average(flow, weights=tm)), 1)
    total = float(tm.sum()) or 1.0
    if hot:
        over = flow > hot * 1.05
        share = float(tm[over].sum()) / total
        if share > 0.01:
            ctx.add("flow", 3, "Asks for more plastic than the hot end can melt",
                    f"For {share:.0%} of the printing time the moves ask for more than {hot:g} mm³/s (up to "
                    f"{peak:.0f} mm³/s), the most the {ctx.label} hot end melts. Those lines come out thin "
                    "and weak, and the extruder may click or grind.",
                    f"Set the filament's max volumetric speed to {min(hot, mat_max or hot):g} mm³/s in the slicer, "
                    "or lower the speeds (often infill).", layer=int(t.layer[peak_i]), at=_where(ctx, peak_i),
                    image="flow", share=round(share, 3))
            return
        if over.any():
            ctx.add("flow", 2, "A few moves exceed the hot end's melt rate",
                    f"Some moves ask for up to {peak:.0f} mm³/s; the {ctx.label} melts about {hot:g} mm³/s.",
                    "Usually harmless if rare; lower the max volumetric speed if lines look thin.",
                    layer=int(t.layer[peak_i]), image="flow")
            return
    if mat_max:
        over = flow > mat_max * 1.1
        share = float(tm[over].sum()) / total
        if share > 0.05:
            ctx.add("flow", 2, f"Faster than {ctx.material['name']} usually melts",
                    f"For {share:.0%} of the time the moves ask for more than {mat_max:g} mm³/s (up to {peak:.0f}); "
                    f"typical {ctx.material['name']} melts reliably up to about {mat_max:g} mm³/s.",
                    f"Use the filament maker's max volumetric speed (about {mat_max:g} mm³/s), or a high-flow version "
                    "of the filament.", layer=int(t.layer[peak_i]), image="flow", share=round(share, 3))


def check_extrusion(ctx: GcodeContext, header_filament: float | None) -> None:
    machine = float(ctx.printer.get("filament_diameter", 1.75))
    if header_filament and abs(header_filament - machine) > 0.2:
        ctx.add("extrusion", 4, f"Sliced for {header_filament:g} mm filament",
                f"The {ctx.label} uses {machine:g} mm filament; a file sliced for {header_filament:g} mm "
                f"feeds about {(header_filament / machine) ** 2:.1f}x {'too little' if header_filament > machine else 'too much'}.",
                f"Set the filament diameter to {machine:g} mm and re-slice.")
        return
    sel = ctx.extrude & ctx.model & (ctx.xy >= 1.0)
    if sel.sum() < 20:
        return
    ratio = float(np.median(ctx.width[sel])) / ctx.nozzle
    ctx.metrics["median_line_width_mm"] = round(ratio * ctx.nozzle, 2)
    if ratio < 0.55 or ratio > 2.2:
        under = ratio < 0.55
        ctx.add("extrusion", 3, "Lines are much " + ("thinner" if under else "wider") + " than the nozzle",
                f"From the plastic fed and the layer height, the lines are about {ratio * ctx.nozzle:.2f} mm wide with a "
                f"{ctx.nozzle:g} mm nozzle: the file {'under' if under else 'over'}-extrudes by about "
                f"{(1 / ratio if under else ratio):.1f}x. A wrong filament diameter or flow ratio usually causes this.",
                "Check the filament diameter (1.75 mm) and flow ratio (about 0.95-1.0) in the slicer.")


def check_retraction(ctx: GcodeContext) -> None:
    t = ctx.trace
    neg = (t.de < -1e-6) & ctx.model
    if neg.any():
        idx = np.flatnonzero(neg)
        breaks = np.flatnonzero(np.diff(idx) > 1) + 1                       # wipes retract over several moves
        runs = np.split(idx, breaks)
        lengths = np.array([-t.de[r].sum() for r in runs])
        ctx.metrics["retractions"] = int(len(lengths))
        ctx.metrics["retraction_mm"] = round(float(np.median(lengths)), 2)
        direct = ctx.printer.get("extruder", "direct") == "direct"
        limit = 2.5 if direct else 8.0
        typical = float(np.median(lengths))
        if typical > limit:
            ctx.add("retraction", 2, "Long retractions for this extruder",
                    f"Retractions are about {typical:.1f} mm; on a {'direct-drive' if direct else 'Bowden'} extruder "
                    f"more than about {limit:g} mm pulls molten plastic into the cold zone and can jam"
                    + (" (PLA especially)." if direct else "."),
                    "Use 0.4-1.0 mm on direct drive (Bambu default 0.8 mm), 4-6 mm on Bowden.")
    elif ctx.trace.firmware_retracts:
        ctx.metrics["retractions"] = ctx.trace.firmware_retracts
    if ctx.trace.firmware_retracts:
        return
    unretracted = unretracted_travels(ctx)
    long_travel = ctx.travel & ctx.model & (ctx.xy > 3.0)
    n_long = int(long_travel.sum())
    over_air = travels_over_air(ctx, unretracted)
    if len(over_air) >= 5:
        stringy = ctx.material["name"] in ("PETG", "TPU")
        i = int(over_air[0])
        layers = len(set(t.layer[over_air].tolist()))
        ctx.add("retraction", 3 if stringy and layers > 10 else 2, "Travels over open space without retracting",
                f"{len(over_air)} travel moves on {layers} layer(s) cross open space between parts of the print without "
                f"retracting first; the nozzle oozes on the way and leaves strings"
                f"{' (PETG and TPU are prone to this)' if stringy else ''}. ({n_long} travels over 3 mm in total.)",
                "Turn retraction on for travels that cross walls (Bambu: retraction length 0.8 mm, 'Reduce infill "
                "retraction' only skips travels inside infill).", layer=int(t.layer[i]), image="travel")


def unretracted_travels(ctx: GcodeContext) -> np.ndarray:
    """Travels over 3 mm made while the filament is not pulled back (boolean per move)."""
    t = ctx.trace
    pos = np.cumsum(t.de)
    before = pos - t.de
    peak_before = np.concatenate([[0.0], np.maximum.accumulate(pos)[:-1]])
    return ctx.travel & ctx.model & (ctx.xy > 3.0) & ((peak_before - before) < 0.1)


def travels_over_air(ctx: GcodeContext, travels: np.ndarray, cell: float = 0.5, min_air_mm: float = 2.0) -> np.ndarray:
    """Indices of travels that pass over more than `min_air_mm` of space not printed on that layer.

    Travels inside the part (across infill) don't leave visible strings and slicers skip retraction there
    on purpose. The layer's printed area is its extrusion lines, widened by one cell, with enclosed areas
    filled (walls enclose infill gaps). Worked out once per print; `travels` selects from the result.
    """
    if ctx.trace.firmware_retracts or not travels.any():
        return np.zeros(0, dtype=np.int64)
    key = ("over_air", cell, min_air_mm)
    if key not in ctx.cache:
        ctx.cache[key] = _travels_over_air(ctx, unretracted_travels(ctx), cell, min_air_mm)
    found = ctx.cache[key]
    return found[travels[found]]


def _travels_over_air(ctx: GcodeContext, travels: np.ndarray, cell: float, min_air_mm: float) -> np.ndarray:
    from scipy import ndimage

    t = ctx.trace
    out: list[int] = []
    cand = np.flatnonzero(travels)
    ext = np.flatnonzero(ctx.extrude)
    ext = ext[np.argsort(t.layer[ext], kind="stable")]
    ext_layers = t.layer[ext]
    for layer in np.unique(t.layer[cand]):
        sel = ext[np.searchsorted(ext_layers, layer):np.searchsorted(ext_layers, layer, side="right")]
        mine = cand[t.layer[cand] == layer]
        if len(sel) < 2:
            continue
        x0, y0 = min(t.x0[sel].min(), t.x1[sel].min()) - 2, min(t.y0[sel].min(), t.y1[sel].min()) - 2
        x1, y1 = max(t.x0[sel].max(), t.x1[sel].max()) + 2, max(t.y0[sel].max(), t.y1[sel].max()) + 2
        w, h = int((x1 - x0) / cell) + 2, int((y1 - y0) / cell) + 2
        if w * h > 4_000_000:
            continue
        grid = np.zeros((h, w), bool)
        px, py = _samples(t.x0[sel], t.y0[sel], t.x1[sel], t.y1[sel], cell / 2)
        grid[((py - y0) / cell).astype(int), ((px - x0) / cell).astype(int)] = True
        grid = ndimage.binary_fill_holes(ndimage.binary_dilation(grid, iterations=1))
        qx, qy, which = _samples(t.x0[mine], t.y0[mine], t.x1[mine], t.y1[mine], cell, with_segment=True)
        ix, iy = ((qx - x0) / cell).astype(int), ((qy - y0) / cell).astype(int)
        ok = (ix >= 0) & (iy >= 0) & (ix < w) & (iy < h)
        empty = ~ok
        empty[ok] = ~grid[iy[ok], ix[ok]]
        air = np.bincount(which, weights=empty, minlength=len(mine)) * cell
        out.extend(mine[air > min_air_mm].tolist())
    return np.array(sorted(out), dtype=np.int64)


def _samples(x0, y0, x1, y1, step: float, with_segment: bool = False):
    """Points every `step` mm along each segment (both ends included), optionally with the segment index."""
    n = np.maximum(1, np.ceil(np.hypot(x1 - x0, y1 - y0) / step).astype(int))
    seg = np.repeat(np.arange(len(n)), n + 1)
    frac = (np.arange(len(seg)) - np.repeat(np.cumsum(n + 1) - (n + 1), n + 1)) / np.repeat(n, n + 1)
    px, py = x0[seg] + (x1 - x0)[seg] * frac, y0[seg] + (y1 - y0)[seg] * frac
    return (px, py, seg) if with_segment else (px, py)


def check_cooling(ctx: GcodeContext) -> None:
    t, mat = ctx.trace, ctx.material
    fan = _at_moves(t.fan_events, len(t), default=0.0)
    sel = ctx.extrude & ctx.model & (t.layer >= 3)
    if sel.sum() < 10 or "fan_pct" not in mat:
        return
    lo, hi = (x / 100 for x in mat["fan_pct"])
    used = fan[sel]
    ctx.metrics["fan_pct"] = [round(float(used.min()) * 100), round(float(used.max()) * 100)]
    if lo >= 0.5 and float(used.max()) < 0.3:
        ctx.add("cooling", 3, f"Part-cooling fan stays off for {mat['name']}",
                f"The fan is at most {used.max():.0%} after the first layers; {mat['name']} wants {lo:.0%}-{hi:.0%}. "
                "Overhangs droop, bridges sag and small details melt.",
                f"Turn the part-cooling fan on from layer 2 at {lo:.0%} or more.")
    elif hi <= 0.4 and float(np.median(used)) > hi + 0.2:
        ctx.add("cooling", 2, f"A lot of cooling for {mat['name']}",
                f"The fan runs at about {np.median(used):.0%}; {mat['name']} wants {lo:.0%}-{hi:.0%}. Strong cooling "
                "makes it warp and the layers split.", f"Keep the fan at {hi:.0%} or less (more only for bridges).")
    elif lo >= 0.5:
        layer_time = np.bincount(t.layer[ctx.model & (t.layer >= 3)].clip(0),
                                 weights=ctx.time[ctx.model & (t.layer >= 3)])
        quick = np.flatnonzero((layer_time > 0) & (layer_time < 3.0))
        if len(quick) >= 3:
            ctx.add("cooling", 2, "Layers too quick to cool",
                    f"{len(quick)} layers take under 3 s (from layer {quick[0] + 1}); each new layer lands on soft "
                    "plastic and small parts sag or curl.",
                    "Raise the slicer's minimum layer time (Bambu: 'Slow printing down for better layer cooling'), or "
                    "print two copies at once.", layer=int(quick[0]))


def check_adhesion(ctx: GcodeContext) -> None:
    t, mat = ctx.trace, ctx.material
    if mat.get("warp_risk") == "high" and not ctx.printer.get("enclosed"):
        ctx.add("adhesion", 3, f"{mat['name']} on an open printer",
                f"{mat['name']} shrinks a lot as it cools; on the open {ctx.label} large parts usually warp "
                "and the layers split.", "Print in an enclosure (or a draught-free box), or switch to PETG.")
    first = np.flatnonzero(~np.isnan(ctx.layer_h))
    if not len(first):
        return
    sel = ctx.extrude & (t.layer == first[0])
    if sel.sum() < 4:
        return
    w = float(np.ptp(np.concatenate([t.x0[sel], t.x1[sel]])))
    d = float(np.ptp(np.concatenate([t.y0[sel], t.y1[sel]])))
    height = float(np.nanmax(ctx.layer_z))
    ctx.metrics["footprint_mm"] = [round(w, 1), round(d, 1)]
    brim = any("brim" in f or "raft" in f for f in t.features)
    ratio = height / max(min(w, d), 0.1)
    if ratio > 4 and not brim:
        ctx.add("adhesion", 3 if ratio > 8 else 2, "Tall and narrow without a brim",
                f"The part is {height:.0f} mm tall on a footprint of {w:.0f} x {d:.0f} mm ({ratio:.0f}x taller than it is "
                "wide); the nozzle can knock it over late in the print.",
                "Add a brim (5-8 mm), or print it lying down.", layer=int(first[0]), image="layer1")


def check_end(ctx: GcodeContext) -> None:
    t = ctx.trace
    sel = np.flatnonzero(ctx.extrude & ctx.model)
    if not len(sel):
        return
    last = int(sel[-1])
    nozzle_off = any(i > last and v == 0 for i, v, _ in t.nozzle_events)
    bed_on = any(v > 0 for _, v in t.bed_events)
    bed_off = any(i > last and v == 0 for i, v in t.bed_events) or not bed_on
    if not (nozzle_off and bed_off):
        left = " and ".join(x for x, off in (("nozzle", nozzle_off), ("bed", bed_off)) if not off)
        ctx.add("end", 2, "Heaters stay on after the print",
                f"The end of the file doesn't switch the {left} off (M104 S0 / M140 S0). Some printers time out, many "
                "keep heating until someone notices.", "Use the printer's own end G-code in the slicer.")


def check_consistency(ctx: GcodeContext, slicer_mm: float | None, slicer_g: float | None, density: float) -> None:
    t = ctx.trace
    used = float(t.de.sum())
    grams = used * ctx.fil_area / 1000 * density
    ctx.metrics["filament_m"] = round(used / 1000, 2)
    ctx.metrics["filament_g"] = round(grams, 1)
    loops = ctx.metrics.get("loops", 1)
    if loops > 1:
        # a loop file carries one slicer header per loop; compare the whole file with all of them
        ctx.metrics["filament_g_per_loop"] = round(grams / loops, 1)
        slicer_mm = _sum_header(t.header_all, ("total filament length [mm]", "filament used [mm]"), loops) or slicer_mm
        slicer_g = _sum_header(t.header_all, ("total filament weight [g]", "filament used [g]"), loops) or (
            slicer_g * loops if slicer_g else None)
    for label, theirs, ours, unit in (("filament length", slicer_mm, used, "mm"), ("weight", slicer_g, grams, "g")):
        if theirs and ours > 0 and abs(theirs - ours) / max(theirs, ours) > 0.15:
            ctx.add("consistency", 2, "The file doesn't match its own header",
                    f"The slicer's header says {theirs:.0f} {unit} of {label}; the moves in the file use {ours:.0f} {unit}. "
                    "The file may have been edited after slicing, cut short, or the header belongs to another plate.",
                    "Re-slice and export again if you didn't edit it on purpose.")
            return


def _sum_header(values: dict[str, list[str]], keys: tuple[str, ...], loops: int) -> float | None:
    for key in keys:
        found = [v for v in values.get(key, []) if re.search(r"\d", v)]
        if not found:
            continue
        nums = [float(re.search(r"-?\d+(\.\d+)?", v.split(",")[0]).group()) for v in found]
        return sum(nums) if len(nums) == loops else nums[0] * loops
    return None


# ----------------------------------------------------------------------------------------- slicer notes

def parse_duration(text: str | None) -> float | None:
    if not text:
        return None
    text = str(text)
    if re.fullmatch(r"\s*\d+(\.\d+)?\s*", text):
        return float(text)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", text)
    if not parts:
        return None
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(float(v) * mult[u] for v, u in parts)


def slicer_notes(header: dict[str, str], plate_info: dict[str, Any]) -> dict[str, Any]:
    def first(*keys: str) -> str | None:
        for k in keys:
            if header.get(k):
                return header[k].strip().strip('"')
        return None

    def number(text: str | None) -> float | None:
        if not text:
            return None
        m = re.search(r"-?\d+(\.\d+)?", text.split(",")[0].split(";")[0])
        return float(m.group()) if m else None

    est = None
    combined = header.get("model printing time", "")
    if "total estimated time" in combined:
        est = parse_duration(combined.split("total estimated time", 1)[1])
    est = est or parse_duration(first("total estimated time", "estimated printing time (normal mode)",
                                      "estimated printing time")) or parse_duration(first("time"))
    est = est or plate_info.get("prediction_s")
    length = number(first("total filament length [mm]", "filament used [mm]"))
    cura_m = first("filament used")
    if length is None and cura_m and cura_m.strip().endswith("m"):
        length = number(cura_m) * 1000 if number(cura_m) is not None else None
    filament_type = first("filament_type")
    if not filament_type and plate_info.get("filaments"):
        filament_type = plate_info["filaments"][0].get("type")
    return {
        "printer": first("printer_model", "printer_settings_id", "machine_name"),
        "nozzle": number(first("nozzle_diameter", "extruder_train.0.nozzle.diameter")) or
        number(str(plate_info.get("nozzle_diameters") or "")),
        "filament_type": filament_type.split(";")[0].strip() if filament_type else None,
        "filament_diameter": number(first("filament_diameter")),
        "time_s": est,
        "filament_mm": length,
        "filament_g": number(first("total filament weight [g]", "filament used [g]")) or plate_info.get("weight_g"),
        "slicer": next((ln for ln in header if ln.lower().startswith(("bambustudio", "orcaslicer", "prusaslicer"))), None),
    }


CLEARLOOP_LEVELS = {"danger": 4, "warning": 3, "info": 1}


def is_loop_file(trace: Any, loops: int) -> bool:
    name = Path(trace.source or trace.path or "").name
    return (loops > 1 or trace.marks.get("clearloop_blocks", 0) > 0 or trace.marks.get("farmloop_sections", 0) > 0
            or name.startswith(("FL_S1_", "FL_S2_")))


def check_loop_file(ctx: GcodeContext) -> None:
    """Loop files (FarmLoop / ClearLoop auto-eject): what the file does, plus ClearLoop's own checks.

    The print checks above already look at each loop's print only; the ejection cycle after it
    (cooldown, door, bend, sweep, plate-check pause) is ClearLoop's to check, so its checker is run
    on the file when ClearLoop is installed.
    """
    t = ctx.trace
    loops = ctx.metrics.get("loops", 1)
    if not is_loop_file(t, loops):
        return
    plates = {(a, b) for a, b in zip(t.header_all.get("model label id", []) or [""] * loops,
                                    t.header_all.get("total layer number", []) or [""] * loops)}
    made_by = ("ClearLoop" if t.marks.get("clearloop_blocks") else
               "the FarmLoop app" if t.marks.get("farmloop_sections") else "another loop tool")
    info: dict[str, Any] = {"loops": loops, "plates": max(1, len(plates)), "made_by": made_by,
                            "plate_check_pauses": t.marks.get("loop_pauses", 0), "clearloop_checks": None}
    ctx.metrics["loop_file"] = info
    path = t.path or ""
    if not path.lower().endswith(".3mf"):
        return
    try:
        from clearloop import threemf as cl_threemf
        from clearloop.lint import lint as cl_lint
    except ImportError:
        heated_waits_before_print(ctx)
        ctx.add("loop_file", 1, "Install ClearLoop to check the ejection cycle",
                f"This is a {loops}-loop file made by {made_by}. The print in each loop was checked; the cooldown, "
                "door, bend and sweep after it were not.",
                'pip install "synthux[clearloop]" (or install ClearLoop) and check the file again.')
        return
    try:
        found = cl_lint(cl_threemf.load(path, t.plate))
    except Exception as exc:  # noqa: BLE001 - a ClearLoop failure must not hide the print checks
        ctx.add("loop_file", 1, "ClearLoop couldn't read this file", str(exc))
        return
    info["clearloop_checks"] = [{"level": f.level, "title": f.title, "detail": f.detail} for f in found]
    for f in found:
        if f.level in CLEARLOOP_LEVELS:
            ctx.add("loop_file", CLEARLOOP_LEVELS[f.level], f"ClearLoop: {f.title}", f.detail,
                    "Make the loop file again with ClearLoop (clearloop make), which fixes the known problems.")


def heated_waits_before_print(ctx: GcodeContext) -> None:
    """A long wait in the start code with a heater on (FarmLoop app 2.72's 'natural cooldown' bug)."""
    t = ctx.trace
    if t.phase is None or not t.dwell_events:
        return
    n = len(t)
    noz = _at_moves([(i, v) for i, v, _ in t.nozzle_events], n + 1, default=0.0)
    bed = _at_moves(t.bed_events, n + 1, default=0.0)
    phase = np.append(t.phase, t.phase[-1] if n else 1)
    loop = np.append(t.loop, t.loop[-1] if n else 0)
    waits: dict[int, float] = {}
    for i, sec in t.dwell_events:
        i = min(i, n)
        if phase[i] == 0 and (noz[i] > 5 or bed[i] > 5):    # 1-5 °C are FarmBoard signals, not heat
            waits[int(loop[i])] = waits.get(int(loop[i]), 0.0) + sec
    worst = max(waits.values(), default=0.0)
    if worst >= 600:
        ctx.add("loop_file", 4, f"{worst / 60:.0f} min wait before printing, heaters on",
                "The start code waits with the nozzle or bed heating before the print begins: the printer looks "
                "frozen and stays hot (the FarmLoop app 2.72 'natural cooldown' placement bug).",
                "Don't run this file. Make it again with the cooldown after the print (ClearLoop does this).")


def check_history(ctx: GcodeContext) -> None:
    """What real prints on this printer + material taught (see synthux.physical.learn)."""
    from datetime import datetime

    from synthux.physical.learn import CAUSE_TEXT, job_key

    h = ctx.history
    if not h or not h.get("prints"):
        return
    m, mat = ctx.metrics, ctx.material["name"]
    who = f"your {ctx.label}"
    peak = m.get("peak_flow_mm3_s")
    ctx.metrics["history"] = {k: h.get(k) for k in ("prints", "finished", "failed", "by_cause", "proven_flow",
                                                    "failed_flow", "eject_checked", "eject_left")}
    if peak and h.get("failed_flow") and peak >= 0.95 * h["failed_flow"]:
        ctx.add("history", 3, f"Prints have failed on {who} at this flow",
                f"This file asks for up to {peak:.0f} mm³/s. {h['flow_failures']} {mat} print(s) on {who} failed on "
                f"layers asking for about {h['failed_flow']:.0f} mm³/s.",
                f"Set the filament's max volumetric speed to about {0.85 * h['failed_flow']:.0f} mm³/s for this printer.",
                image="flow")
    elif peak and h.get("proven_flow") and peak <= h["proven_flow"]:
        for f in ctx.findings:
            if f.check == "flow":
                f.severity = 1
                f.title += f" (but {who} prints it fine)"
                f.detail += (f" Still, {h['proven_prints']} {mat} prints on {who} finished at up to "
                             f"{h['proven_flow']:.0f} mm³/s, so it handles this flow.")
    firsts = h["by_cause"].get("first_layer", 0)
    if firsts >= 2 and firsts / max(h["prints"], 1) >= 0.2:
        ctx.add("history", 2, f"{firsts} of your last {h['prints']} {mat} prints failed in the first layers",
                f"On {who}, {firsts} {mat} prints stopped in the first three layers: the first layer isn't sticking "
                "reliably on this printer.",
                "Wash the plate with dish soap and water, check the plate type matches the slicer, raise the bed "
                "5 °C, and slow the first layer.", layer=0, image="layer1")
    name = job_key(ctx.trace.source.split(" (plate")[0])
    for prev in [j for j in h.get("job") or [] if j["outcome"] == "failed"][:1]:
        when = datetime.fromtimestamp(prev["ended_at"]).strftime("%d %b %Y")
        where = f"layer {prev['layer']}" + (f" of loop {prev['loop_no']}" if prev.get("loop_no") else "")
        why = CAUSE_TEXT.get(prev["cause"] or "unknown", "")
        hms = f" The printer reported HMS {', '.join(prev['hms'])}." if prev.get("hms") else ""
        ctx.add("history", 3, f"This file failed before, at {where}",
                f"{name} failed on {who} on {when}, at {where}, {why}.{hms}",
                "Look at that layer in the preview, and fix the cause before printing it again.")
    left = [j for j in h.get("job") or [] if j.get("eject") == "parts_left"]
    if left:
        ctx.add("history", 2, f"Parts were left on the plate {len(left)} time(s)",
                f"After {len(left)} print(s) of {name}, the plate check found parts still on the plate: the sweep "
                "didn't push everything off.",
                "Let the bed cool further before the sweep (lower release temperature), or push that part lower "
                "in ClearLoop.")


def loop_times(header_all: dict[str, list[str]]) -> list[float]:
    """Slicer time of each loop's print, from each loop's own header."""
    out = []
    for v in header_all.get("model printing time", []):
        tail = v.split("total estimated time", 1)
        sec = parse_duration(tail[1] if len(tail) > 1 else v)
        if sec:
            out.append(sec)
    return out


def run_gcode_checks(ctx: GcodeContext, notes: dict[str, Any]) -> GcodeContext:
    prepare(ctx)
    check_bounds(ctx)
    check_printer(ctx, notes.get("printer"), notes.get("nozzle"))
    check_layers(ctx)
    check_temperatures(ctx)
    check_flow(ctx)
    check_extrusion(ctx, notes.get("filament_diameter"))
    check_retraction(ctx)
    check_cooling(ctx)
    check_adhesion(ctx)
    check_end(ctx)
    check_consistency(ctx, notes.get("filament_mm"), notes.get("filament_g"),
                      float(ctx.material.get("density_g_cm3", 1.24)))
    check_loop_file(ctx)
    check_history(ctx)
    replay = float(ctx.time[ctx.model].sum())                    # the prints themselves
    ctx.metrics["replay_time_s"] = round(replay)
    ctx.metrics["slicer_time_s"] = round(notes["time_s"]) if notes.get("time_s") else None
    loops = ctx.metrics.get("loops", 1)
    if loops > 1:
        per_loop = loop_times(ctx.trace.header_all)
        if per_loop:
            ctx.metrics["slicer_time_s"] = round(sum(per_loop) if len(per_loop) == loops else per_loop[0] * loops)
        ctx.metrics["dwell_s"] = round(ctx.trace.dwell_s)
    ctx.metrics["travel_m"] = round(float(ctx.xy[ctx.travel].sum()) / 1000, 1)
    ctx.metrics["tool_changes"] = ctx.trace.tool_changes
    feats = {}
    for i, name in enumerate(ctx.trace.features):
        s = ctx.extrude & (ctx.trace.feature == i) & ctx.model
        if s.any():
            feats[name] = round(float(ctx.time[s].sum()) / 60, 1)
    ctx.metrics["minutes_by_feature"] = dict(sorted(feats.items(), key=lambda kv: -kv[1]))
    if ctx.trace.tool_changes:
        ctx.add("consistency", 1, f"{ctx.trace.tool_changes} filament changes",
                "Each change purges filament (often 1-3 g each with an AMS) and adds about a minute.",
                "Group colours by layer, or print a single colour, to save filament.")
    ctx.findings.sort(key=lambda f: -f.severity)
    from synthux.physical.gcode.settings_fixes import fixes_for

    fixes_for(ctx)
    return ctx
