"""Checks on temperatures, melt rate, line width (filament diameter) and cooling."""

from __future__ import annotations

import numpy as np

from proofloop.check.gcode.context import COLD_EXTRUSION_C, GcodeContext, _at_moves, _where


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
