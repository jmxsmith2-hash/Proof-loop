"""Overhangs, print orientation and bed contact."""

from __future__ import annotations

import math

import numpy as np
import trimesh

from proofloop.check.cad.context import CadFinding, Context, _face_components, _loc


def overhang_mask(normals: np.ndarray, centroids_z: np.ndarray, min_z: float, max_deg: float,
                  layer: float) -> np.ndarray:
    """Faces that point down more steeply than the material can print without support."""
    down = -normals[:, 2]
    limit = math.sin(math.radians(max_deg))   # angle measured from vertical
    off_bed = centroids_z > min_z + layer * 1.5
    return (down > limit) & off_bed


def support_columns(ctx: Context, mask: np.ndarray) -> float:
    """Volume (mm³) of the support columns under the overhanging faces: from each face down to the bed or to
    the part below it (measured on the voxel grid)."""
    grid, mesh = ctx.grid, ctx.mesh
    if grid is None or not mask.any():
        return 0.0
    centres = mesh.triangles_center[mask]
    areas = mesh.area_faces[mask] * np.abs(mesh.face_normals[mask][:, 2])   # footprint seen from below
    idx = np.floor((centres - grid.origin) / grid.pitch).astype(int)
    shape = np.array(grid.solid.shape)
    idx = np.clip(idx, 0, shape - 1)
    k_below = np.clip(idx[:, 2] - 2, 0, None)   # start just under the face itself
    # index of the highest solid voxel at or below each height in every column (-1 = none, i.e. the bed)
    levels = np.where(grid.solid, np.arange(shape[2], dtype=np.int32)[None, None, :], -1)
    top = np.maximum.accumulate(levels, axis=2)
    below = top[idx[:, 0], idx[:, 1], k_below]
    floor_z = np.where(below >= 0, grid.origin[2] + (below + 1) * grid.pitch, mesh.bounds[0][2])
    heights = np.clip(centres[:, 2] - floor_z, 0, None)
    return float((areas * heights).sum())


def check_overhangs(ctx: Context) -> None:
    mesh = ctx.mesh
    mat = ctx.material
    mask = overhang_mask(mesh.face_normals, mesh.triangles_center[:, 2], mesh.bounds[0][2],
                         mat["max_overhang_deg"], ctx.printer["layer_height"])
    if ctx.exterior is not None:
        mask &= ctx.exterior
    total = float(mesh.area_faces[mask].sum())
    ctx.metrics["overhang_area_mm2"] = round(total, 1)
    ctx.metrics["support_column_mm3"] = round(support_columns(ctx, mask), 1)
    regions = []
    for faces in _face_components(mesh, mask):
        area = float(mesh.area_faces[faces].sum())
        if area < 4.0:
            continue
        pts = mesh.triangles[faces].reshape(-1, 3)
        centre = np.average(mesh.triangles_center[faces], axis=0, weights=mesh.area_faces[faces])
        worst = float(np.degrees(np.arcsin(np.clip(-mesh.face_normals[faces][:, 2], -1, 1))).max())
        flat = float((mesh.area_faces[faces][-mesh.face_normals[faces][:, 2] > 0.98]).sum()) / area
        xy = pts[:, :2] - pts[:, :2].mean(axis=0)
        span = float(2 * np.sqrt(max(np.linalg.eigvalsh(np.cov(xy.T))[0], 0)) * 1.73) if len(xy) > 2 else 0.0
        regions.append((area, centre, worst, flat, span))
    for area, centre, worst, flat, span in sorted(regions, key=lambda r: -r[0])[:6]:
        if flat > 0.8 and span <= mat["max_bridge_mm"]:
            ctx.add(CadFinding("bridge", 1 if span < mat["max_bridge_mm"] * 0.6 else 2,
                               "Flat underside (bridge or ledge)",
                               f"A flat downward face of {area:.0f} mm², about {span:.0f} mm across. Fine as a bridge "
                               f"if both ends are supported (limit about {mat['max_bridge_mm']} mm for {mat['name']}); "
                               "a ledge supported on one side only will sag.",
                               "If it is a one-sided ledge: add a 45° chamfer or gusset underneath, or enable supports.",
                               _loc(centre, span / 2), {"area_mm2": round(area, 1), "span_mm": round(span, 1)}))
        else:
            sev = 3 if area > 25 else 2
            ctx.add(CadFinding("overhang", sev, f"Overhang at {worst:.0f}° needs support",
                               f"{area:.0f} mm² of surface faces down at up to {worst:.0f}° from vertical "
                               f"(limit about {mat['max_overhang_deg']}° for {mat['name']}). It will droop or fail "
                               "without supports.",
                               "Change the print orientation, add a 45° chamfer under it, or enable supports "
                               "(and expect support marks there).",
                               _loc(centre, math.sqrt(area / math.pi)), {"area_mm2": round(area, 1),
                                                                          "angle_deg": round(worst, 1)}))


ORIENTATIONS = {
    "as modelled": np.eye(3),
    "upside down": trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0])[:3, :3],
    "on its front (90° about X)": trimesh.transformations.rotation_matrix(math.pi / 2, [1, 0, 0])[:3, :3],
    "on its back (-90° about X)": trimesh.transformations.rotation_matrix(-math.pi / 2, [1, 0, 0])[:3, :3],
    "on its right side (90° about Y)": trimesh.transformations.rotation_matrix(math.pi / 2, [0, 1, 0])[:3, :3],
    "on its left side (-90° about Y)": trimesh.transformations.rotation_matrix(-math.pi / 2, [0, 1, 0])[:3, :3],
}


def score_orientation(mesh: trimesh.Trimesh, rot: np.ndarray, material: dict, printer: dict,
                      exterior: np.ndarray | None = None) -> dict[str, float]:
    normals = mesh.face_normals @ rot.T
    centres = mesh.triangles_center @ rot.T
    verts = mesh.vertices @ rot.T
    min_z = verts[:, 2].min()
    mask = overhang_mask(normals, centres[:, 2], min_z, material["max_overhang_deg"], printer["layer_height"])
    on_bed = (centres[:, 2] <= min_z + printer["layer_height"]) & (normals[:, 2] < -0.99)
    if exterior is not None:
        mask &= exterior
        on_bed &= exterior
    overhang = float(mesh.area_faces[mask].sum())
    contact = float(mesh.area_faces[on_bed].sum())
    size = verts.max(axis=0) - verts.min(axis=0)
    fits = bool(np.all(size <= np.array(printer["build_volume"])))
    score = overhang - 0.5 * min(contact, 600) + 0.3 * size[2] + (0 if fits else 1e6)
    return {"overhang_mm2": round(overhang, 1), "contact_mm2": round(contact, 1), "height_mm": round(float(size[2]), 1),
            "fits": fits, "score": round(score, 1)}


def check_orientation(ctx: Context) -> None:
    candidates = dict(ORIENTATIONS)
    hull = ctx.mesh.convex_hull
    facets = sorted(zip(hull.facets_area, hull.facets_normal, strict=False), key=lambda f: -f[0])[:4] if len(hull.facets) else []
    for n, (_area, normal) in enumerate(facets):
        rot = trimesh.geometry.align_vectors(normal, [0, 0, -1])[:3, :3]
        candidates[f"resting on its largest flat face #{n + 1}"] = rot
    scored = {name: score_orientation(ctx.mesh, rot, ctx.material, ctx.printer, ctx.exterior)
              for name, rot in candidates.items()}
    current = scored["as modelled"]
    best_name = min(scored, key=lambda k: scored[k]["score"])
    best = scored[best_name]
    ctx.metrics["orientation"] = {"current": current, "best": {"name": best_name, **best}}
    saving = current["overhang_mm2"] - best["overhang_mm2"]
    if best_name != "as modelled" and saving > max(40.0, 0.3 * current["overhang_mm2"]):
        ctx.add(CadFinding("orientation", 2, "A different print orientation needs far less support",
                           f"Printed {best_name}, overhangs drop from {current['overhang_mm2']:.0f} to "
                           f"{best['overhang_mm2']:.0f} mm² and bed contact goes from {current['contact_mm2']:.0f} to "
                           f"{best['contact_mm2']:.0f} mm².",
                           f"Print it {best_name}. Check that layer lines then run along, not across, any part "
                           "that takes load.", None, {"best": best_name, **best}))


def check_bed(ctx: Context) -> None:
    current = ctx.metrics.get("orientation", {}).get("current") or score_orientation(
        ctx.mesh, np.eye(3), ctx.material, ctx.printer, ctx.exterior)
    contact, height = current["contact_mm2"], current["height_mm"]
    ctx.metrics["bed_contact_mm2"] = contact
    lo = ctx.mesh.bounds[0]
    base = _loc(np.array([ctx.mesh.centroid[0], ctx.mesh.centroid[1], lo[2]]))
    if contact < 1.0:
        ctx.add(CadFinding("bed_contact", 3, "Nothing sits flat on the bed",
                           "No flat face touches the print bed as modelled; the first layer has nothing to hold on to.",
                           "Add a flat base, print in another orientation, or use a raft/supports.", base))
    elif height / math.sqrt(contact) > 8 and height > 25:
        ctx.add(CadFinding("bed_contact", 3, "Tall part on a small footprint",
                           f"{height:.0f} mm tall on only {contact:.0f} mm² of bed contact; the nozzle can knock it "
                           "loose or it wobbles near the top.", "Add a brim, widen the base, or print lying down.", base))
    footprint = float(max(ctx.mesh.extents[0], ctx.mesh.extents[1]))
    if ctx.material.get("warp_risk") == "high" and footprint > 100 and contact > 2000:
        ctx.add(CadFinding("warping", 2, f"Large flat {ctx.material['name']} part may warp",
                           f"{footprint:.0f} mm across in {ctx.material['name']}, which shrinks as it cools; corners "
                           "tend to lift.", "Use an enclosure, a brim, rounded corners, or a lower-shrink material.", base))
