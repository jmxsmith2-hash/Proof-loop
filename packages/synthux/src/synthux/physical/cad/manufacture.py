"""Checks for products that are moulded or machined rather than printed.

Injection moulding (two-part mould, straight pull):
* undercuts: empty space with part material both above and below it along the pull can't be
  formed by either mould half; it needs a side action (if it opens sideways) or a redesign;
* draft: side walls parallel to the pull drag on the mould when the part is pushed out;
* wall thickness: too thin doesn't fill, too thick sinks and voids, uneven walls warp;
* sharp inside corners: stress points that also slow the flow;
* estimates: shot weight, clamp force from the projected area, cooling and cycle time.

CNC machining (3-axis, re-clamped from up to six sides):
* reach: material that no straight tool can reach from any side (needs 5-axis or a redesign),
  and the fewest set-ups that reach the rest;
* tool size: inside corners sharper than the tool's radius, slots narrower than the tool;
* deep narrow pockets, thin walls, deep or tiny holes;
* estimates: stock size, share removed, rough machining time.

Everything is measured on the same voxel grid as the print checks; figures come from
config/processes.yaml and are typical design-guide values.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage

from synthux.core.config import ConfigError, load_yaml_file
from synthux.core.logging import get_logger
from synthux.physical.cad.checks import (CadFinding, Context, _loc, check_clearances, check_mesh, check_sharp_edges,
                                         check_stability, exterior_faces, find_holes)
from synthux.physical.cad.load import Model
from synthux.physical.cad.voxel import VoxelGrid, choose_pitch, voxelize

log = get_logger(__name__)

PROCESSES = {"injection": "Injection moulding", "cnc": "CNC machining (3-axis)"}
AXES = {"x": 0, "y": 1, "z": 2}


def load_process(config_dir: Path, process: str, material: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if process not in PROCESSES:
        raise ConfigError(f"unknown process {process!r}; choose one of {sorted(PROCESSES)} (or leave it out to print)")
    data = (load_yaml_file(Path(config_dir) / "processes.yaml") or {}).get(process)
    if not data:
        raise ConfigError(f"no '{process}' section in {Path(config_dir) / 'processes.yaml'}")
    mats = data["materials"]
    name = material or data["default_material"]
    match = next((m for m in mats if m.lower() == str(name).lower()), None)
    if match is None:
        raise ConfigError(f"unknown {process} material {name!r}; known: {sorted(mats)}")
    spec = {k: v for k, v in data.items() if k != "materials"}
    return {"name": PROCESSES[process], "kind": process, **spec}, {"name": match, **mats[match]}


def _context(model: Model, process: dict, material: dict, use: dict, pitch: float) -> Context:
    # The shared product checks (sharp edges, clearances, stability) read a few print-profile fields.
    mat = {"min_wall_mm": material.get("min_wall_mm", (material.get("wall_mm") or [1.0])[0]),
           "clearance_mm": 0.1 if process["kind"] == "injection" else 0.05, **material}
    printer = {"name": process["name"], "type": process["kind"], "nozzle": pitch, "layer_height": pitch,
               "build_volume": [1e6, 1e6, 1e6]}
    ctx = Context(model=model, printer=printer, material=mat, slicer={}, use=use)
    ctx.metrics["voxel_mm"] = choose_pitch(model.mesh.extents, pitch)
    ctx.grid = voxelize(model.mesh, ctx.metrics["voxel_mm"])
    ctx.exterior = exterior_faces(model.mesh, ctx.grid)
    return ctx


def _clusters(mask: np.ndarray, grid: VoxelGrid, min_mm3: float) -> tuple[list[dict[str, Any]], np.ndarray]:
    labels, n = ndimage.label(mask, structure=np.ones((3, 3, 3)))
    out = []
    for k, sl in enumerate(ndimage.find_objects(labels), 1):
        if sl is None:
            continue
        region = labels[sl] == k
        volume = float(region.sum()) * grid.pitch ** 3
        if volume < min_mm3:
            continue
        idx = np.argwhere(region) + np.array([s.start for s in sl])
        out.append({"volume": volume, "centre": grid.centre(idx.mean(axis=0)), "slice": sl, "label": k,
                    "extent": np.array([s.stop - s.start for s in sl]) * grid.pitch})
    out.sort(key=lambda c: -c["volume"])
    return out, labels


def _thickness(grid: VoxelGrid) -> tuple[np.ndarray, np.ndarray]:
    """Wall thickness at the middle of each wall (ridge of the distance map): (ridge mask, thickness mm)."""
    dist = ndimage.distance_transform_edt(np.pad(grid.solid, 1))[1:-1, 1:-1, 1:-1]
    ridge = grid.solid & (dist >= ndimage.maximum_filter(dist, size=3) - 1e-6) & (dist > 0)
    return ridge, np.maximum(grid.pitch, (2 * dist - 0.5) * grid.pitch)


def _visible(empty: np.ndarray, axis: int, from_high: bool) -> np.ndarray:
    """Empty voxels with a clear straight line to the outside along an axis (towards + if from_high)."""
    solid = (~empty).astype(np.int32)
    if from_high:
        ahead = np.flip(np.cumsum(np.flip(solid, axis), axis), axis)   # solid at or beyond this voxel
    else:
        ahead = np.cumsum(solid, axis)
    return empty & (ahead == 0)


# ------------------------------------------------------------------------------- injection moulding

def _trapped(solid: np.ndarray, axis: int) -> np.ndarray:
    above = np.flip(np.cumsum(np.flip(solid, axis), axis), axis) > 0
    below = np.cumsum(solid, axis) > 0
    return ~solid & above & below


def best_pull(grid: VoxelGrid) -> str:
    """The pull direction with the least undercut volume (ties go to Z, then X)."""
    scores = {name: int(_trapped(grid.solid, ax).sum()) for name, ax in (("z", 2), ("x", 0), ("y", 1))}
    return min(scores, key=lambda k: scores[k])


def check_undercuts(ctx: Context, pull: str) -> None:
    grid = ctx.grid
    ax = AXES[pull]
    trapped = _trapped(grid.solid, ax)
    clusters, labels = _clusters(trapped, grid, min_mm3=max(2.0, 8 * grid.pitch ** 3))
    empty = ~grid.solid
    side = {f"{'+' if hi else '-'}{name.upper()}": _visible(empty, a, hi)
            for name, a in AXES.items() if a != ax for hi in (True, False)}
    outside_air = None
    actions = 0
    for c in clusters[:6]:
        region = labels[c["slice"]] == c["label"]
        opens = [d for d, vis in side.items() if (vis[c["slice"]] & region).any()]
        size = " x ".join(f"{v:.0f}" for v in c["extent"])
        if opens:
            actions += 1
            ctx.add(CadFinding("undercut", 2, f"Undercut needs a side action from {opens[0]}",
                               f"About {c['volume']:.0f} mm³ ({size} mm) of the mould has part material both above "
                               f"and below it along the pull ({pull.upper()}), so neither half can form it. It opens "
                               f"sideways ({', '.join(opens)}), so a side action (slider) can, at extra mould cost.",
                               "Redesign it so it opens along the pull (a through-hole or a pass-through core), "
                               "or accept a side action.", _loc(c["centre"], float(c["extent"].max()) / 2),
                               {"volume_mm3": round(c["volume"], 1), "opens": opens}))
        else:
            if outside_air is None:                       # empty space connected to the air around the part
                air, _ = ndimage.label(empty)
                outside_air = air == air[0, 0, 0]
            sealed = not (outside_air[c["slice"]] & region).any()
            ctx.add(CadFinding("undercut", 4 if sealed else 3,
                               "Enclosed space the mould can't form" if sealed else "Undercut with no way out",
                               f"About {c['volume']:.0f} mm³ ({size} mm) of empty space is closed in by the part "
                               f"along the pull ({pull.upper()}) and from the sides; no mould half or side action "
                               "can be pulled out of it.",
                               "Split the part into two moulded halves, open the space to one side, or make it solid.",
                               _loc(c["centre"], float(c["extent"].max()) / 2), {"volume_mm3": round(c["volume"], 1)}))
    ctx.metrics["undercuts"] = len(clusters)
    ctx.metrics["side_actions"] = actions


def check_draft(ctx: Context, pull: str, min_deg: float) -> None:
    mesh = ctx.mesh
    d = np.zeros(3)
    d[AXES[pull]] = 1.0
    n = mesh.face_normals
    along = np.abs(n @ d)
    side = along < math.sin(math.radians(45))                     # walls more or less along the pull
    if ctx.exterior is not None and len(ctx.exterior) == len(mesh.faces):
        side &= ctx.exterior
    low = side & (along < math.sin(math.radians(min_deg)) - 1e-9)
    side_area = float(mesh.area_faces[side].sum())
    low_area = float(mesh.area_faces[low].sum())
    ctx.metrics["no_draft_share"] = round(low_area / side_area, 3) if side_area else 0.0
    if side_area == 0 or low_area < 0.05 * side_area or low_area < 20:
        return
    none = float(mesh.area_faces[side & (along < 1e-4)].sum())
    worst = mesh.triangles_center[low][np.argmax(mesh.area_faces[low])]
    ctx.add(CadFinding("draft", 3 if none > 0.5 * side_area else 2,
                       "Side walls have no draft" if none > 0.5 * side_area else "Some side walls lack draft",
                       f"{low_area / side_area:.0%} of the side walls ({low_area / 100:.0f} cm²) are within {min_deg:g}° "
                       f"of the pull direction ({pull.upper()}); they rub on the mould as the part is pushed out, "
                       "leaving drag marks or sticking.",
                       f"Tilt side walls by at least {min_deg:g}° (about 3° on textured surfaces), away from the "
                       "side the mould opens.", _loc(worst, 6.0),
                       {"share": round(low_area / side_area, 3), "area_mm2": round(low_area, 1)}))


def check_walls(ctx: Context, lo: float, hi: float, *, process: str) -> np.ndarray:
    grid = ctx.grid
    ridge, thick = _thickness(grid)
    values = thick[ridge]
    if not values.size:
        return values
    ctx.metrics["wall_mm"] = {"min": round(float(np.percentile(values, 2)), 2),
                              "typical": round(float(np.median(values)), 2),
                              "max": round(float(np.percentile(values, 98)), 2)}
    thin_c, _ = _clusters(ridge & (thick < lo * 0.95), grid, min_mm3=4 * grid.pitch ** 3)
    for c in thin_c[:2]:
        t = float(thick[c["slice"]][ridge[c["slice"]]].min())
        if process == "injection":
            ctx.add(CadFinding("wall_thickness", 3, f"Wall too thin to fill: about {t:.1f} mm",
                               f"Walls about {t:.1f} mm thick; in {ctx.material['name']} plastic freezes before it fills "
                               f"walls under about {lo:g} mm (a short shot), especially far from the gate.",
                               f"Make walls at least {lo:g} mm, or move the gate closer.", _loc(c["centre"], 4.0),
                               {"thickness_mm": round(t, 2)}))
        else:
            ctx.add(CadFinding("wall_thickness", 2, f"Thin wall: about {t:.1f} mm",
                               f"Walls about {t:.1f} mm thick; in {ctx.material['name']} walls under about {lo:g} mm "
                               "vibrate and bend under the cutter, so they come out wavy or break.",
                               f"Make them at least {lo:g} mm, or support them while machining.",
                               _loc(c["centre"], 4.0), {"thickness_mm": round(t, 2)}))
    if process == "injection":
        thick_c, _ = _clusters(ridge & (thick > hi * 1.05), grid, min_mm3=4 * grid.pitch ** 3)
        for c in thick_c[:2]:
            t = float(thick[c["slice"]][ridge[c["slice"]]].max())
            ctx.add(CadFinding("wall_thickness", 3 if t > 2 * hi else 2, f"Thick section: about {t:.1f} mm",
                               f"A section about {t:.1f} mm thick (over {hi:g} mm for {ctx.material['name']}) cools last: "
                               "it shrinks into a dip on the surface (sink mark) or a hole inside, and lengthens "
                               "the cycle.", "Core it out to an even wall, with ribs (60% of the wall) for stiffness.",
                               _loc(c["centre"], 5.0), {"thickness_mm": round(t, 2)}))
        p10, p90 = np.percentile(values, [10, 90])
        if p10 > 0 and p90 / p10 > 2.5 and not thick_c:
            ctx.add(CadFinding("wall_thickness", 2, "Uneven wall thickness",
                               f"Walls vary from about {p10:.1f} to {p90:.1f} mm; thick and thin areas shrink differently "
                               "and the part warps.", "Keep walls within about ±25% of one thickness; change gradually."))
    return values


def check_inside_corners(ctx: Context, wall: float) -> None:
    mesh = ctx.mesh
    if len(mesh.face_adjacency) == 0:
        return
    angles = mesh.face_adjacency_angles
    concave = ~mesh.face_adjacency_convex & (angles > math.radians(50))
    if not concave.any():
        return
    edges = mesh.vertices[mesh.face_adjacency_edges[concave]]
    lengths = np.linalg.norm(edges[:, 0] - edges[:, 1], axis=1)
    total = float(lengths.sum())
    if total < 5:
        return
    where = edges[np.argmax(lengths)].mean(axis=0)
    ctx.add(CadFinding("inside_corner", 1, "Sharp inside corners",
                       f"About {total:.0f} mm of inside edges meet at a sharp angle; plastic flows badly round them and "
                       "they concentrate stress, so cracks start there.",
                       f"Round inside corners to at least {0.5 * wall:.1f} mm (half the wall), outside ones to "
                       f"{1.5 * wall:.1f} mm.", _loc(where, 4.0), {"length_mm": round(total, 1)}))


def injection_estimates(ctx: Context, pull: str, walls: np.ndarray) -> None:
    grid, mat, proc = ctx.grid, ctx.material, ctx.printer
    volume = grid.volume_mm3
    grams = volume * mat["density_g_cm3"] / 1000
    projected = float(grid.solid.any(axis=AXES[pull]).sum()) * grid.pitch ** 2
    tonnes = projected * mat["cavity_mpa"] / 9810                   # mm² x MPa = N
    wall = float(np.percentile(walls, 90)) if walls.size else 2.0
    cool = mat["cool_s_per_mm2"] * wall ** 2
    cycle = cool + float(proc.get("cycle_overhead_s", 6))
    ctx.metrics["estimates"] = {
        "solid_volume_cm3": round(volume / 1000, 2), "printed_grams": round(grams, 1), "support_grams": 0,
        "material_cost": round(grams * mat["price_per_kg"] / 1000, 3), "print_hours": None,
        "process_stats": [
            [f"{grams:.1f} g", "shot weight (one part)"],
            [f"{tonnes:.0f} t", f"clamp force ({projected / 100:.0f} cm² projected along {pull.upper()})"],
            [f"{cycle:.0f} s", f"cycle (about {cool:.0f} s cooling for {wall:.1f} mm walls)"],
            [f"{mat['shrink_pct']}%", "shrinkage to allow for in the mould"]],
        "assumptions": f"{mat['name']}, {mat['cavity_mpa']} MPa cavity pressure, pull along {pull.upper()}"}


# ------------------------------------------------------------------------------------------- CNC

def _to_remove(grid: VoxelGrid) -> np.ndarray:
    """Empty space inside the part's bounding box: what is cut away from a block cut to size."""
    box = np.zeros_like(grid.solid)
    idx = np.argwhere(grid.solid)
    if len(idx):
        lo, hi = idx.min(axis=0), idx.max(axis=0) + 1
        box[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]] = True
    return box & ~grid.solid


def check_reach(ctx: Context) -> list[str]:
    """Which material (inside the stock box) a straight tool reaches from each of the six sides."""
    grid = ctx.grid
    empty = ~grid.solid
    remove = _to_remove(grid)
    ctx.cache["cnc_remove"] = remove
    vis = {f"{'+' if hi else '-'}{name.upper()}": _visible(empty, a, hi) for name, a in AXES.items() for hi in (True, False)}
    reach = np.zeros_like(empty)
    for v in vis.values():
        reach |= v
    blocked = remove & ~reach
    clusters, _ = _clusters(blocked, grid, min_mm3=max(2.0, 8 * grid.pitch ** 3))
    for c in clusters[:3]:
        ctx.add(CadFinding("tool_reach", 3, "No straight tool can reach this",
                           f"About {c['volume']:.0f} mm³ of material to remove is hidden from all six sides (an "
                           "undercut or an enclosed pocket); a 3-axis machine can't cut it, even after re-clamping.",
                           "Open it to one side, split the part, or plan for 5-axis machining or a T-slot cutter.",
                           _loc(c["centre"], float(c["extent"].max()) / 2), {"volume_mm3": round(c["volume"], 1)}))
    # fewest set-ups: greedily add the side that reaches the most remaining material near the part
    left = remove & reach
    total = int(left.sum()) or 1
    chosen: list[str] = []
    while left.sum() > 0.005 * total and len(chosen) < 6:
        best = max(vis, key=lambda k: int((vis[k] & left).sum()))
        if not (vis[best] & left).any():
            break
        chosen.append(best)
        left &= ~vis[best]
    ctx.metrics["setups"] = chosen
    if len(chosen) > 2:
        ctx.add(CadFinding("setups", 2 if len(chosen) <= 4 else 3, f"Needs {len(chosen)} set-ups",
                           f"The part has to be clamped {len(chosen)} times ({', '.join(chosen)}) to reach every face; "
                           "each set-up adds cost and a chance of misalignment.",
                           "Put features on as few sides as you can (one or two is cheapest)."))
    ctx.cache["cnc_vis"] = {k: vis[k] for k in chosen}
    return chosen


def check_tool_size(ctx: Context, tool: float, min_tool: float) -> None:
    """Inside corners and slots a round end mill can't follow, seen along each set-up direction."""
    grid = ctx.grid
    vis = ctx.cache.get("cnc_vis", {})
    if not vis:
        return
    r_px = tool / 2 / grid.pitch
    if r_px < 1:
        return
    disc = _disc2(r_px)
    small = _disc2(min_tool / 2 / grid.pitch)
    cut_std = np.zeros_like(grid.solid)
    cut_min = np.zeros_like(grid.solid)
    for name, v in vis.items():
        ax = AXES[name[1].lower()]
        for k in range(v.shape[ax]):
            sl = [slice(None)] * 3
            sl[ax] = k
            sl = tuple(sl)
            plane = v[sl]
            if not plane.any():
                continue
            cut_std[sl] |= _open_with_air(plane, disc)
            cut_min[sl] |= _open_with_air(plane, small) if small.shape[0] > 1 else plane
    reached = np.zeros_like(grid.solid)
    for v in vis.values():
        reached |= v
    remove = ctx.cache.get("cnc_remove", _to_remove(grid))
    left_std = reached & ~cut_std & remove
    left_min = reached & ~cut_min & remove
    # What a round tool leaves behind: a thin line along an inside corner, or a thin sheet in a slot
    # narrower than the tool (long in two directions).
    def sheet(c: dict[str, Any], limit: float) -> bool:
        return float(np.sort(c["extent"])[-2]) >= limit

    bodies = ndimage.label(grid.solid, structure=np.ones((3, 3, 3)))[0]

    def between_parts(c: dict[str, Any], labels: np.ndarray) -> bool:
        """A gap between two separate bodies is not a slot: they are machined one at a time."""
        big = tuple(slice(max(0, s.start - 2), s.stop + 2) for s in c["slice"])
        region = labels[big] == c["label"]
        near = bodies[big][ndimage.binary_dilation(region, iterations=2)]
        return len(np.unique(near[near > 0])) >= 2

    std, std_labels = _clusters(left_std, grid, min_mm3=grid.pitch ** 3 * 4)
    std = [c for c in std if not between_parts(c, std_labels)]
    corners = [c for c in std if not sheet(c, 2 * tool)]
    tight = [c for c in std if sheet(c, 2 * tool)]
    mins, min_labels = _clusters(left_min, grid, min_mm3=grid.pitch ** 3 * 8)
    narrow = [c for c in mins if sheet(c, 2 * min_tool) and not between_parts(c, min_labels)]
    if corners:
        c = corners[0]
        ctx.add(CadFinding("tool_radius", 1, f"Inside corners sharper than a {tool:g} mm tool",
                           f"{len(corners)} inside corner(s) are sharper than a {tool:g} mm end mill can follow; it "
                           f"leaves a {tool / 2:g} mm radius there, or a smaller, slower tool is needed.",
                           f"Give inside vertical corners a radius of at least {tool / 2 + 0.2:.1f} mm (a bit more than "
                           "the tool's), or relieve them with dog-bone cuts if a square part must fit.",
                           _loc(c["centre"], 3.0), {"places": len(corners)}))
    if narrow:
        c = narrow[0]
        ctx.add(CadFinding("tool_radius", 3, f"Narrower than the smallest tool ({min_tool:g} mm)",
                           f"{len(narrow)} slot(s) or gap(s) are narrower than a {min_tool:g} mm end mill can enter; "
                           "they can't be milled (only by EDM or a redesign).",
                           f"Make slots and gaps at least {min_tool * 1.5:g} mm wide.",
                           _loc(c["centre"], float(c["extent"].max()) / 2), {"places": len(narrow)}))
    elif tight:
        c = tight[0]
        ctx.add(CadFinding("tool_radius", 2, f"Slots narrower than a {tool:g} mm tool",
                           f"{len(tight)} slot(s) are narrower than a {tool:g} mm end mill; they need a smaller tool, "
                           "which cuts slower and breaks more easily.",
                           f"Make slots at least {tool * 1.2:.1f} mm wide where you can.",
                           _loc(c["centre"], float(c["extent"].max()) / 2), {"places": len(tight)}))


def _open_with_air(plane: np.ndarray, disc: np.ndarray) -> np.ndarray:
    """Where a round tool fits, with open air (not a wall) beyond the edge of the grid."""
    pad = disc.shape[0]
    return ndimage.binary_opening(np.pad(plane, pad, constant_values=True), disc)[pad:-pad, pad:-pad]


def _disc2(r: float) -> np.ndarray:
    k = int(math.ceil(r))
    ax = np.arange(-k, k + 1)
    return (ax[:, None] ** 2 + ax[None, :] ** 2) <= r * r + 1e-9


def check_pockets(ctx: Context, ratio: float) -> None:
    """Pockets much deeper than they are wide need long, thin tools that chatter (from the main set-up)."""
    grid = ctx.grid
    setups = ctx.metrics.get("setups") or ["+Z"]
    name = setups[0]
    ax = AXES[name[1].lower()]
    from_high = name[0] == "+"
    solid = grid.solid if from_high else np.flip(grid.solid, ax)
    solid = np.moveaxis(solid, ax, 2)                     # tool comes down from high z
    any_solid = solid.any(axis=2)
    top = np.where(any_solid, solid.shape[2] - 1 - np.argmax(solid[:, :, ::-1], axis=2), -1)
    surface = top.max()
    depth = np.where(any_solid, (surface - top) * grid.pitch, 0.0)
    pocket = (depth > 2 * grid.pitch) & any_solid
    labels, n = ndimage.label(pocket)
    if n == 0:
        return
    found = []
    for k, sl in enumerate(ndimage.find_objects(labels), 1):
        if sl is None:
            continue
        big = tuple(slice(max(0, s.start - 1), s.stop + 1) for s in sl)
        region = labels[big] == k
        ring = ndimage.binary_dilation(region) & ~region
        if not any_solid[big][ring].all():
            continue                                      # open to the outside: a step or profile, not a pocket
        rows, cols = np.nonzero(region)
        bw, bd = (np.ptp(rows) + 1) * grid.pitch, (np.ptp(cols) + 1) * grid.pitch
        if max(bw, bd) < 1.3 * min(bw, bd) and region.sum() * grid.pitch ** 2 > 0.6 * math.pi / 4 * bw * bd:
            continue                                      # a round blind hole: the hole check covers it
        width = 2 * float(ndimage.distance_transform_edt(np.pad(region, 1)).max()) * grid.pitch
        deep = float(depth[big][region].max())
        if width > 0 and deep / width > ratio and deep > 3:
            ij = np.argwhere(region).mean(axis=0) + np.array([s.start for s in big])
            found.append((deep / width, deep, width, ij))
    others = [a for a in range(3) if a != ax]
    for r, deep, width, ij in sorted(found, key=lambda f: -f[0])[:2]:
        idx = np.zeros(3)
        idx[others[0]], idx[others[1]] = ij[0], ij[1]
        idx[ax] = surface if from_high else grid.solid.shape[ax] - 1 - surface
        ctx.add(CadFinding("deep_pocket", 3 if r > 2 * ratio else 2, f"Pocket {deep:.0f} mm deep but {width:.0f} mm wide",
                           f"A pocket about {deep:.0f} mm deep and {width:.0f} mm across (machined from {name}) is "
                           f"{r:.0f}x deeper than wide; it needs a long, thin tool that bends and chatters, so it is "
                           "slow and the walls come out poor.", f"Keep pockets under {ratio:g}x their width deep, or "
                           "widen them.", _loc(grid.centre(idx), max(width, 3.0)),
                           {"depth_mm": round(deep, 1), "width_mm": round(width, 1)}))


def check_cnc_holes(ctx: Context, max_ratio: float, min_tool: float) -> None:
    grid = ctx.grid
    for axis in (2, 0, 1):
        for h in find_holes(grid, axis):
            dia, depth = h["diameter"], h["depth"]
            if dia < 3 * grid.pitch:
                continue
            if depth / dia > max_ratio:
                ctx.add(CadFinding("deep_hole", 2, f"Deep {dia:.1f} mm hole ({depth / dia:.0f}x its diameter)",
                                   f"A hole about {dia:.1f} mm across and {depth:.0f} mm deep along {h['axis'].upper()}; "
                                   f"over about {max_ratio:g}x the diameter drills wander and break.",
                                   "Make it shorter or wider, or drill from both sides.", _loc(h["centre"], dia),
                                   {"diameter_mm": round(dia, 2), "depth_mm": round(depth, 1)}))
            elif dia < min_tool:
                ctx.add(CadFinding("deep_hole", 2, f"Tiny {dia:.1f} mm hole",
                                   f"A hole about {dia:.1f} mm across; drills that small break easily.",
                                   f"Use at least {min_tool:g} mm.", _loc(h["centre"], 2.0), {"diameter_mm": round(dia, 2)}))


def cnc_estimates(ctx: Context) -> None:
    grid, mat = ctx.grid, ctx.material
    size = ctx.mesh.extents + 2.0                         # 1 mm to face off on every side
    stock = float(np.prod(size))
    part = grid.volume_mm3
    removed = max(0.0, stock - part)
    minutes = removed / 1000 / mat["removal_cm3_min"] + float(ctx.mesh.area) / 100 / 30 \
        + 10 * len(ctx.metrics.get("setups") or [1])      # roughing + finishing + set-up handling
    ctx.metrics["estimates"] = {
        "solid_volume_cm3": round(part / 1000, 2), "printed_grams": round(part * mat["density_g_cm3"] / 1000, 1),
        "support_grams": 0, "print_hours": None,
        "material_cost": round(stock * mat["density_g_cm3"] / 1000 * mat["price_per_kg"] / 1000, 2),
        "process_stats": [
            [" x ".join(f"{v:.0f}" for v in size) + " mm", "stock block"],
            [f"{removed / stock:.0%}", "of the stock is cut away"],
            [f"{minutes:.0f} min", "rough machining time"],
            [", ".join(ctx.metrics.get("setups") or ["+Z"]), "set-ups (sides to clamp)"]],
        "assumptions": f"{mat['name']}, roughing at {mat['removal_cm3_min']} cm³/min"}


# ------------------------------------------------------------------------------------------- run

def run_process_checks(model: Model, process: dict, material: dict, use: dict | None = None,
                       pull: str | None = None) -> Context:
    kind = process["kind"]
    walls = material.get("wall_mm") or [material.get("min_wall_mm", 1.0), 1e9]
    pitch = min(max(0.2, walls[0] / 3), 0.5)
    if kind == "cnc":   # fine enough that the smallest tool is a proper disc (5 cells across)
        pitch = min(pitch, float(process.get("tool_mm", 3.0)) / 6, float(process.get("min_tool_mm", 1.0)) / 4)
    ctx = _context(model, process, material, use or {}, pitch)
    checks: list = [check_mesh]
    if kind == "injection":
        chosen = (pull or "auto").lower()
        if chosen == "auto":
            chosen = best_pull(ctx.grid)
        if chosen not in AXES:
            raise ConfigError("--pull must be x, y, z or auto")
        ctx.metrics["pull"] = chosen
        checks += [lambda c: check_undercuts(c, chosen),
                   lambda c: check_draft(c, chosen, float(process.get("min_draft_deg", 1.0))),
                   lambda c: c.cache.__setitem__("walls", check_walls(c, walls[0], walls[1], process="injection")),
                   lambda c: check_inside_corners(c, float(np.median(c.cache.get("walls", np.array([2.0]))) or 2.0))]
    else:
        checks += [check_reach,
                   lambda c: check_tool_size(c, float(process.get("tool_mm", 3)), float(process.get("min_tool_mm", 1))),
                   lambda c: check_pockets(c, float(process.get("max_depth_per_width", 4))),
                   lambda c: check_walls(c, float(material["min_wall_mm"]), 1e9, process="cnc"),
                   lambda c: check_cnc_holes(c, float(process.get("max_hole_depth_per_dia", 10)),
                                             float(process.get("min_tool_mm", 1)))]
    checks += [check_sharp_edges, check_clearances, check_stability]
    for check in checks:
        try:
            check(ctx)
        except Exception as exc:  # one failing check must not hide the others
            name = getattr(check, "__name__", "check")
            log.warning("process check %s failed: %s", name, exc)
            ctx.add(CadFinding("check_error", 1, "Could not run a check", str(exc)))
    if kind == "injection":
        injection_estimates(ctx, ctx.metrics["pull"], ctx.cache.get("walls", np.zeros(0)))
    else:
        cnc_estimates(ctx)
    ctx.findings.sort(key=lambda f: -f.severity)
    return ctx
