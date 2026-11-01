"""Checks on the plate, the printer, the first layer and layer heights, adhesion and the end code."""

from __future__ import annotations

import numpy as np

from proofloop.check.gcode.context import GcodeContext, _where


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
