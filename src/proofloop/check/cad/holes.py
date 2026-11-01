"""Holes: small vertical holes, horizontal holes and sealed resin pockets."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy import ndimage

from proofloop.check.cad.context import CadFinding, Context, _loc
from proofloop.check.cad.voxel import VoxelGrid

TYPICAL_FDM_HOLE_OFFSET = -0.15   # FDM holes usually print about this much (mm) smaller than modelled
FIT_HOLE_MAX_MM = 8.0             # holes up to this size are usually for screws, pins and inserts


def find_vertical_holes(grid: VoxelGrid) -> list[dict[str, Any]]:
    """Round-ish empty areas enclosed by material in the layers (holes printed layer by layer)."""
    return find_holes(grid, axis=2)


def find_holes(grid: VoxelGrid, axis: int = 2) -> list[dict[str, Any]]:
    """Round-ish empty areas enclosed by material in planes across `axis`, through at least two planes.

    axis 2 (Z) finds holes printed layer by layer; axis 0 or 1 finds horizontal holes along X or Y.
    """
    order = {2: (0, 1, 2), 0: (1, 2, 0), 1: (0, 2, 1)}[axis]
    solid = np.transpose(grid.solid, order)
    back = np.argsort(order)
    enclosed = np.zeros_like(solid)
    for k in range(1, solid.shape[2] - 1):
        empty = ~solid[:, :, k]
        if empty.all():
            continue
        labels, n = ndimage.label(empty)
        if n < 2:
            continue
        edge = np.unique(np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]))
        enclosed[:, :, k] = (labels > 0) & ~np.isin(labels, edge)
    if not enclosed.any():
        return []
    labels3, n3 = ndimage.label(enclosed)          # the same hole through many planes
    holes = []
    for k, sl in enumerate(ndimage.find_objects(labels3), 1):
        if sl is None:
            continue
        region = labels3[sl] == k
        per_layer = region.sum(axis=(0, 1))
        layers = per_layer[per_layer > 0]
        if len(layers) < 2:
            continue
        w, d = (sl[0].stop - sl[0].start) * grid.pitch, (sl[1].stop - sl[1].start) * grid.pitch
        area = float(np.median(layers)) * grid.pitch ** 2
        round_enough = max(w, d) <= 1.6 * max(min(w, d), grid.pitch) and area >= 0.55 * (math.pi / 4) * w * d
        if not round_enough:
            continue                                # a slot or gap, not a hole
        coords = (np.argwhere(region) + np.array([s.start for s in sl]))[:, back]
        holes.append({"diameter": 2 * math.sqrt(area / math.pi), "depth": len(layers) * grid.pitch,
                      "centre": grid.centre(coords.mean(axis=0)), "axis": "xyz"[axis]})
    return holes


def check_holes(ctx: Context) -> None:
    grid, printer, mat = ctx.grid, ctx.printer, ctx.material
    if grid is None:
        return
    holes = find_vertical_holes(grid)
    ctx.metrics["holes"] = [round(h["diameter"], 2) for h in holes]
    if printer.get("type", "fdm") == "fdm":
        learned_offset = mat.get("hole_offset_mm")
        offset = float(learned_offset) if learned_offset is not None else TYPICAL_FDM_HOLE_OFFSET
        source = (f"your prints showed holes coming out {abs(offset):.2f} mm "
                  f"{'small' if offset < 0 else 'large'}" if learned_offset is not None
                  else f"FDM holes usually print about {abs(TYPICAL_FDM_HOLE_OFFSET):.2f} mm small")
        closes = max(1.0, 2.5 * printer["nozzle"])
        reported = 0
        for h in sorted(holes, key=lambda h: h["diameter"]):
            dia = h["diameter"]
            if dia >= FIT_HOLE_MAX_MM or reported >= 4 or grid.pitch > dia / 3:
                continue                    # big holes don't need a fit; holes under 3 voxels can't be measured
            reported += 1
            if dia < closes:
                ctx.add(CadFinding("small_hole", 3, f"{dia:.1f} mm hole may close up",
                                   f"A vertical hole about {dia:.1f} mm across; with a {printer['nozzle']} mm nozzle, "
                                   f"holes under about {closes:.1f} mm often close or come out much too small.",
                                   f"Model it at least {closes + abs(offset):.1f} mm, or drill it out after printing.",
                                   _loc(h["centre"], max(dia, 3.0)), {"diameter_mm": round(dia, 2)}))
            elif offset < 0:
                nominal = round(dia, 1)   # the size the designer meant (the voxel measurement is +-0.05 mm)
                ctx.add(CadFinding("hole_fit", 1, f"{nominal:.1f} mm hole will print about {nominal + offset:.1f} mm",
                                   f"A vertical hole about {nominal:.1f} mm across; {source}.",
                                   f"For a {nominal:.1f} mm fit (screw, pin, insert), model it at "
                                   f"{nominal - offset:.2f} mm.",
                                   _loc(h["centre"], max(dia, 3.0)),
                                   {"diameter_mm": round(dia, 2), "offset_mm": round(offset, 3)}))
    else:
        check_resin_traps(ctx)


def check_horizontal_holes(ctx: Context) -> None:
    """Holes along X or Y: their top is a flat overhang that sags, so they print oval or need a teardrop."""
    grid, printer, mat = ctx.grid, ctx.printer, ctx.material
    if grid is None or printer.get("type", "fdm") != "fdm":
        return
    holes = [h for axis in (0, 1) for h in find_holes(grid, axis) if h["diameter"] >= 3 * grid.pitch]
    ctx.metrics["horizontal_holes"] = [round(h["diameter"], 2) for h in holes]
    bridge = float(mat.get("max_bridge_mm", 20))
    groups: dict[tuple[str, float], list[dict[str, Any]]] = {}
    for h in holes:
        groups.setdefault(("fit" if h["diameter"] < FIT_HOLE_MAX_MM else "large", round(h["diameter"])), []).append(h)
    reported = 0
    for (kind, _), hs in sorted(groups.items(), key=lambda kv: -max(h["diameter"] for h in kv[1])):
        if reported >= 3:
            break
        reported += 1
        dia = float(np.median([h["diameter"] for h in hs]))
        many = f"{len(hs)} horizontal holes" if len(hs) > 1 else "A horizontal hole"
        where = _loc(hs[0]["centre"], max(dia, 3.0))
        data = {"diameter_mm": round(dia, 2), "count": len(hs), "axis": hs[0]["axis"]}
        if kind == "fit":
            ctx.add(CadFinding("horizontal_hole", 1, f"Horizontal {dia:.1f} mm hole prints oval",
                               f"{many} about {dia:.1f} mm across run{'' if len(hs) > 1 else 's'} sideways (along "
                               f"{hs[0]['axis'].upper()}). The top of each is printed in mid-air, so it sags about "
                               "0.2-0.3 mm and the hole comes out oval and tight for a pin or bolt.",
                               "Give the hole a teardrop top (a 45° point upwards) or model it 0.3 mm taller, "
                               "and drill or ream it to size if it is a fit.", where, data))
        else:
            severe = dia > bridge
            ctx.add(CadFinding("horizontal_hole", 3 if severe else 2, f"Top of a {dia:.0f} mm horizontal hole sags",
                               f"{many} about {dia:.0f} mm across run{'' if len(hs) > 1 else 's'} sideways; the top of "
                               f"the hole is a flat overhang about {dia * 0.6:.0f} mm wide that droops"
                               + (f", wider than {mat['name']} bridges cleanly ({bridge:g} mm)." if severe else "."),
                               "Use a teardrop or diamond-shaped top so the roof is 45° and needs no support, "
                               "or turn the part so the hole is vertical.", where, data))


def check_resin_traps(ctx: Context) -> None:
    """Resin printing: a hollow pocket sealed inside the part traps liquid resin and can crack the print."""
    grid = ctx.grid
    if grid is None:
        return
    labels, n = ndimage.label(~grid.solid)
    if n < 2:
        return
    outside = labels[0, 0, 0]
    sizes = ndimage.sum_labels(np.ones_like(labels), labels, np.arange(1, n + 1))
    for k in range(1, n + 1):
        volume = float(sizes[k - 1]) * grid.pitch ** 3
        if k == outside or volume < 5.0:
            continue
        centre = grid.centre(np.argwhere(labels == k).mean(axis=0))
        ctx.add(CadFinding("resin_trap", 3, "Sealed hollow pocket traps resin",
                           f"A closed cavity of about {volume:.0f} mm³ inside the part has no opening; uncured resin "
                           "stays inside and the pressure difference can crack or cup the print.",
                           "Add two drain/vent holes (2-3 mm) near the lowest and highest point of the cavity, "
                           "or make that region solid.", _loc(centre, 4.0), {"volume_mm3": round(volume, 1)}))
