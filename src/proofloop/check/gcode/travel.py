"""Checks on retraction and on travel moves that cross open space."""

from __future__ import annotations

import numpy as np

from proofloop.check.gcode.context import GcodeContext


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
