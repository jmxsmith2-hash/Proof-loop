"""Design-for-printing and usability checks computed from the CAD geometry.

These are measurements of your model, not predictions about people. Each
finding has a location in millimetres (model coordinates), a severity, and a
suggested fix. Severity: 1 note, 2 minor, 3 major, 4 will fail.

The checks live in one module per family (mesh, overhang, walls, holes, clearance, fine_detail,
stability, eject); this module runs them in order and re-exports their names.
"""

from __future__ import annotations

import math
from typing import Any

from proofloop.check.cad.clearance import TOUCH_MM, check_clearances, point_triangle_distance, shell_gap
from proofloop.check.cad.context import (
    SEVERITY,
    CadFinding,
    Context,
    _face_components,
    _loc,
    exterior_faces,
    fingerprint,
)
from proofloop.check.cad.eject import check_auto_eject
from proofloop.check.cad.fine_detail import check_detail
from proofloop.check.cad.holes import (
    FIT_HOLE_MAX_MM,
    TYPICAL_FDM_HOLE_OFFSET,
    check_holes,
    check_horizontal_holes,
    check_resin_traps,
    find_holes,
    find_vertical_holes,
)
from proofloop.check.cad.load import Model
from proofloop.check.cad.mesh import check_build_volume, check_mesh, check_sharp_edges
from proofloop.check.cad.overhang import (
    ORIENTATIONS,
    check_bed,
    check_orientation,
    check_overhangs,
    overhang_mask,
    score_orientation,
    support_columns,
)
from proofloop.check.cad.stability import check_stability
from proofloop.check.cad.voxel import choose_pitch, voxelize
from proofloop.check.cad.walls import (
    check_cantilevers,
    check_slender,
    check_thin,
    coarse_grid,
)
from proofloop.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "FIT_HOLE_MAX_MM", "ORIENTATIONS", "SEVERITY", "TOUCH_MM", "TYPICAL_FDM_HOLE_OFFSET", "CadFinding", "Context",
    "_face_components", "_loc", "check_auto_eject", "check_bed", "check_build_volume", "check_cantilevers", "check_clearances",
    "check_detail", "check_holes", "check_horizontal_holes", "check_mesh", "check_orientation", "check_overhangs",
    "check_resin_traps", "check_sharp_edges", "check_slender", "check_stability", "check_thin", "coarse_grid",
    "estimates", "exterior_faces", "find_holes", "find_vertical_holes", "fingerprint", "overhang_mask",
    "point_triangle_distance", "run_checks", "score_orientation", "shell_gap", "support_columns",
]

SUPPORT_DENSITY = 0.15   # sparse tree/grid support fills about this share of the column it stands in


def estimates(ctx: Context) -> dict[str, Any]:
    mesh, mat, sl = ctx.mesh, ctx.material, ctx.slicer
    volume = ctx.grid.volume_mm3 if ctx.grid is not None else abs(float(mesh.volume))
    shell_t = sl.get("perimeters", 2) * ctx.printer["nozzle"] + 0.2
    shell = min(volume, float(mesh.area) * shell_t)
    infill = sl.get("infill_pct", 15) / 100
    printed = shell + (volume - shell) * infill
    support = ctx.metrics.get("support_column_mm3", 0.0) * SUPPORT_DENSITY
    grams = printed * mat["density_g_cm3"] / 1000                 # the part itself (support is removed)
    used = (printed + support) * mat["density_g_cm3"] / 1000       # filament used, support included
    fil_d = sl.get("filament_diameter", 1.75)
    metres = (printed + support) / (math.pi * (fil_d / 2) ** 2) / 1000
    hours = (printed + support) / ctx.printer["volumetric_speed_mm3_s"] * 1.35 / 3600
    est = {"solid_volume_cm3": round(volume / 1000, 2), "printed_grams": round(grams, 1),
           "support_grams": round(support * mat["density_g_cm3"] / 1000, 1),
           "filament_m": round(metres, 2), "print_hours": round(hours, 2),
           "filament_grams": round(used, 1),
           "material_cost": round(used * mat["price_per_kg"] / 1000, 2),
           "assumptions": f"{sl.get('perimeters', 2)} perimeters, {sl.get('infill_pct', 15)}% infill, "
                          f"{ctx.printer['volumetric_speed_mm3_s']} mm³/s"}
    ctx.metrics["estimates"] = est
    return est


def run_checks(model: Model, printer: dict, material: dict, slicer: dict, use: dict | None = None,
               pitch: float | None = None) -> Context:
    ctx = Context(model=model, printer=printer, material=material, slicer=slicer, use=use or {})
    min_wall = max(material["min_wall_mm"], 2 * printer["nozzle"])
    wanted = pitch or min(min_wall / 3, 0.4)
    ctx.metrics["voxel_mm"] = choose_pitch(model.mesh.extents, wanted)
    ctx.grid = voxelize(model.mesh, ctx.metrics["voxel_mm"])
    ctx.exterior = exterior_faces(model.mesh, ctx.grid)
    if ctx.metrics["voxel_mm"] > min_wall / 2:
        ctx.metrics["resolution_note"] = (f"large part: checked at {ctx.metrics['voxel_mm']} mm resolution, so walls "
                                          f"under about {2 * ctx.metrics['voxel_mm']:.1f} mm may be missed")
    for check in (check_mesh, check_build_volume, check_overhangs, check_orientation, check_bed, check_thin,
                  check_cantilevers, check_slender, check_holes, check_horizontal_holes, check_sharp_edges,
                  check_clearances, check_detail, check_stability, check_auto_eject):
        try:
            check(ctx)
        except Exception as exc:  # one failing check must not hide the others
            log.warning("check %s failed: %s", check.__name__, exc)
            ctx.add(CadFinding("check_error", 1, f"Could not run {check.__name__}", str(exc)))
    estimates(ctx)
    ctx.findings.sort(key=lambda f: -f.severity)
    return ctx
