"""Will it stand: centre of mass against the footprint, with any declared load."""

from __future__ import annotations

import math

import numpy as np
from scipy.spatial import ConvexHull

from proofloop.check.cad.context import CadFinding, Context, _loc


def check_stability(ctx: Context) -> None:
    """In the orientation it is used in (as modelled, Z up), with any declared load."""
    mesh = ctx.mesh
    grid = ctx.grid
    if grid is not None and grid.solid.any():
        com = grid.centre(np.argwhere(grid.solid).mean(axis=0))
        mass = grid.volume_mm3
    else:
        com, mass = mesh.centroid, abs(mesh.volume)
    load = ctx.use.get("load")
    if load:
        density = ctx.material["density_g_cm3"] / 1000
        part_g = mass * density
        load_at = np.array(load.get("at", com), dtype=float)
        com = (com * part_g + load_at * float(load["mass_g"])) / (part_g + float(load["mass_g"]))
    min_z = mesh.bounds[0][2]
    base = mesh.vertices[mesh.vertices[:, 2] <= min_z + 0.5][:, :2]
    ctx.metrics["centre_of_mass_mm"] = [round(float(v), 1) for v in com]
    height = float(com[2] - min_z)
    what = "with its load" if load else "empty"
    if len(base) < 3 or np.ptp(base[:, 0]) < 1e-6 or np.ptp(base[:, 1]) < 1e-6:
        ctx.add(CadFinding("stability", 3, "Does not stand on a base",
                           "It touches the table on a point or a line only, so it cannot stand up on its own.",
                           "Add a flat base if it should stand.", _loc(np.array([com[0], com[1], min_z]))))
        return
    hull = ConvexHull(base)
    eq = hull.equations                      # a*x + b*y + c <= 0 inside
    signed = -(eq[:, :2] @ com[:2] + eq[:, 2])
    margin = float(signed.min())
    tip = math.degrees(math.atan2(margin, max(height, 1e-6)))
    ctx.metrics["tip_angle_deg"] = round(tip, 1)
    spot = _loc(np.array([com[0], com[1], min_z]), max(abs(margin), 5.0))
    if margin < 0:
        ctx.add(CadFinding("stability", 4, f"Falls over {what}",
                           f"The centre of mass ({what}) sits {abs(margin):.0f} mm outside the footprint.",
                           "Widen or extend the base under the heavy side, or move weight lower.", spot,
                           {"tip_angle_deg": round(tip, 1)}))
    elif tip < 8:
        ctx.add(CadFinding("stability", 3, f"Tips over easily {what}",
                           f"A {tip:.0f}° tilt is enough to tip it ({what}); a nudge or a tap on a phone screen will do.",
                           "Widen the base or lower the centre of mass; 15° or more feels solid.", spot,
                           {"tip_angle_deg": round(tip, 1)}))
    elif tip < 15:
        ctx.add(CadFinding("stability", 2, f"Somewhat tippy {what}",
                           f"It tips at about {tip:.0f}° ({what}).", "A slightly wider base would make it feel sturdy.",
                           spot, {"tip_angle_deg": round(tip, 1)}))
