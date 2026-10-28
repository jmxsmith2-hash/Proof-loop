"""Design-for-printing and usability checks computed from the CAD geometry.

These are measurements of your model, not predictions about people. Each
finding has a location in millimetres (model coordinates), a severity, and a
suggested fix. Severity: 1 note, 2 minor, 3 major, 4 will fail.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import trimesh
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import ConvexHull, cKDTree

from synthux.core.logging import get_logger
from synthux.physical.cad.load import Model
from synthux.physical.cad.voxel import VoxelGrid, choose_pitch, voxelize

log = get_logger(__name__)

SEVERITY = {1: "note", 2: "minor", 3: "major", 4: "will fail"}


@dataclass
class CadFinding:
    check: str
    severity: int
    title: str
    detail: str
    fix: str = ""
    location: dict[str, float] | None = None      # {"x","y","z"} centre in mm, plus optional "r" radius
    data: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""

    @property
    def severity_name(self) -> str:
        return SEVERITY[self.severity]


@dataclass
class Context:
    model: Model
    printer: dict[str, Any]
    material: dict[str, Any]
    slicer: dict[str, Any]
    use: dict[str, Any] = field(default_factory=dict)  # handheld, wearable, load, ...
    grid: VoxelGrid | None = None
    findings: list[CadFinding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    exterior: np.ndarray | None = None              # faces on the real outer surface (not buried in another body)
    cache: dict[str, Any] = field(default_factory=dict, repr=False)   # shared intermediate results (distance maps)

    @property
    def mesh(self) -> trimesh.Trimesh:
        return self.model.mesh

    def add(self, finding: CadFinding) -> None:
        if not finding.fingerprint:
            finding.fingerprint = fingerprint(finding, self.mesh.bounds)
        for existing in self.findings:  # one finding per problem and place
            if existing.fingerprint == finding.fingerprint and finding.location:
                if finding.severity > existing.severity:
                    self.findings[self.findings.index(existing)] = finding
                return
        self.findings.append(finding)


def fingerprint(f: CadFinding, bounds: np.ndarray) -> str:
    """Stable id for matching the same problem across design versions (location bucketed to a 4x4x4 grid)."""
    if not f.location:
        return f.check
    lo, hi = bounds
    rel = (np.array([f.location["x"], f.location["y"], f.location["z"]]) - lo) / np.maximum(hi - lo, 1e-6)
    bucket = np.clip((rel * 4).astype(int), 0, 3)
    return f"{f.check}@{bucket[0]}{bucket[1]}{bucket[2]}"


def _loc(point: np.ndarray, radius: float | None = None) -> dict[str, float]:
    out = {"x": round(float(point[0]), 1), "y": round(float(point[1]), 1), "z": round(float(point[2]), 1)}
    if radius is not None:
        out["r"] = round(float(radius), 1)
    return out


def _face_components(mesh: trimesh.Trimesh, mask: np.ndarray) -> list[np.ndarray]:
    """Connected groups of the faces selected by mask (sharing an edge)."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    pairs = mesh.face_adjacency
    keep = mask[pairs[:, 0]] & mask[pairs[:, 1]]
    p = pairs[keep]
    remap = -np.ones(len(mesh.faces), dtype=int)
    remap[idx] = np.arange(idx.size)
    graph = coo_matrix((np.ones(len(p)), (remap[p[:, 0]], remap[p[:, 1]])), shape=(idx.size, idx.size))
    n, labels = connected_components(graph, directed=False)
    return [idx[labels == k] for k in range(n)]


def exterior_faces(mesh: trimesh.Trimesh, grid: VoxelGrid) -> np.ndarray:
    """Faces whose outside is air. Faces buried inside an overlapping body, or pressed against another
    body's surface, are not part of what gets printed and must not be judged."""
    probe = mesh.triangles_center + mesh.face_normals * grid.pitch * 1.2
    return grid.is_outside(probe)


# ---------------------------------------------------------------- mesh health


def check_mesh(ctx: Context) -> None:
    mesh = ctx.mesh
    edges = mesh.edges_sorted.astype(np.int64)
    _, counts = np.unique(edges[:, 0] * (len(mesh.vertices) + 1) + edges[:, 1], return_counts=True)  # one key per edge
    boundary = int((counts == 1).sum())
    nonmanifold = int((counts > 2).sum())
    degenerate = int((mesh.area_faces < 1e-10).sum())
    shells = ctx.model.shells()
    ctx.metrics.update({"triangles": len(mesh.faces), "watertight": bool(mesh.is_watertight),
                        "open_edges": boundary, "non_manifold_edges": nonmanifold, "shells": len(shells),
                        "degenerate_faces": degenerate})
    if boundary:
        ctx.add(CadFinding("mesh_open", 3, "The mesh has holes",
                           f"{boundary} open edges: the surface is not closed, so slicers may guess wrong or skip "
                           "parts, and the volume/weight below are less reliable.",
                           "Re-export as a solid body, or repair (e.g. the slicer's 'repair model' option)."))
    if nonmanifold:
        ctx.add(CadFinding("mesh_non_manifold", 2, "Non-manifold edges",
                           f"{nonmanifold} edges are shared by more than two faces (touching bodies or internal faces).",
                           "Merge touching bodies into one solid, or separate them by at least the material clearance."))
    if not mesh.is_winding_consistent:
        ctx.add(CadFinding("mesh_normals", 2, "Inconsistent face orientation",
                           "Some faces point inward; slicers may fill or hollow the wrong regions.",
                           "Recalculate normals / repair the mesh before slicing."))
    if degenerate:
        ctx.add(CadFinding("mesh_degenerate", 1, "Zero-area triangles",
                           f"{degenerate} degenerate triangles (usually harmless export noise).", "Usually none needed."))
    if ctx.model.units_note:
        ctx.add(CadFinding("units", 2 if "assumed" in ctx.model.units_note else 1, "Check the units",
                           ctx.model.units_note, "Pass --units mm|cm|m|in if the size looks wrong."))


# --------------------------------------------------------------- build volume


def check_build_volume(ctx: Context) -> None:
    size = ctx.mesh.extents
    vol = np.array(ctx.printer["build_volume"], dtype=float)
    ctx.metrics["size_mm"] = [round(float(v), 1) for v in size]
    if np.all(size <= vol):
        return
    if np.all(np.sort(size) <= np.sort(vol)):
        ctx.add(CadFinding("build_volume", 2, "Only fits the printer when rotated",
                           f"{size[0]:.0f} x {size[1]:.0f} x {size[2]:.0f} mm does not fit {ctx.printer['name']} "
                           f"({vol[0]:.0f} x {vol[1]:.0f} x {vol[2]:.0f}) as modelled, but fits on its side.",
                           "Rotate it in the slicer (see the orientation suggestion)."))
    else:
        ctx.add(CadFinding("build_volume", 4, "Too big for the printer",
                           f"{size[0]:.0f} x {size[1]:.0f} x {size[2]:.0f} mm is larger than {ctx.printer['name']} "
                           f"({vol[0]:.0f} x {vol[1]:.0f} x {vol[2]:.0f}) in every orientation.",
                           "Split the part with alignment pins or dovetails, or scale it down."))


# ------------------------------------------------------------------- overhangs


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


# --------------------------------------------------------------- orientation


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
    facets = sorted(zip(hull.facets_area, hull.facets_normal), key=lambda f: -f[0])[:4] if len(hull.facets) else []
    for n, (area, normal) in enumerate(facets):
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


# --------------------------------------------------------------- bed adhesion


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


# -------------------------------------------------------- thin walls, slender


def coarse_grid(ctx: Context, pitch_mm: float) -> VoxelGrid:
    """The solid at a coarser pitch (majority of each block), for checks about features of several mm."""
    grid = ctx.grid
    assert grid is not None
    k = max(1, int(pitch_mm // grid.pitch))
    if k == 1:
        return grid
    key = f"coarse:{k}"
    if key not in ctx.cache:
        nx, ny, nz = (int(math.ceil(d / k)) * k for d in grid.solid.shape)
        padded = np.zeros((nx, ny, nz), dtype=np.float32)
        padded[tuple(slice(0, d) for d in grid.solid.shape)] = grid.solid
        blocks = padded.reshape(nx // k, k, ny // k, k, nz // k, k).mean(axis=(1, 3, 5))
        ctx.cache[key] = VoxelGrid(solid=blocks >= 0.5, origin=grid.origin, pitch=grid.pitch * k)
    return ctx.cache[key]


def _ball(radius: float) -> np.ndarray:
    r = int(math.ceil(radius))
    ax = np.arange(-r, r + 1)
    return (ax[:, None, None] ** 2 + ax[None, :, None] ** 2 + ax[None, None, :] ** 2) <= radius * radius + 1e-9


def _capped_distance(solid: np.ndarray, cap: float) -> np.ndarray:
    """Exact Euclidean distance (in voxels) to the nearest empty voxel, for distances up to `cap`.

    On a voxel grid a distance is always sqrt(integer), so one small erosion per possible value up to the
    cap gives exact values, far faster than a full distance transform when the cap is a few voxels.
    """
    out = np.zeros(solid.shape, dtype=np.float32)
    out[solid] = 1.0
    r = int(math.ceil(cap))
    values = sorted({math.sqrt(i * i + j * j + k * k) for i in range(r + 1) for j in range(r + 1)
                     for k in range(r + 1)} - {0.0})
    for v in values[1:]:
        if v > cap:
            break
        inner = ndimage.binary_erosion(solid, _ball(v - 1e-6))   # every voxel closer than v is solid
        if not inner.any():
            break
        out[inner] = v
    return out


def _thin_components(ctx: Context, limit_mm: float, grid: VoxelGrid | None = None) -> list[dict[str, Any]]:
    """Solid regions thinner than `limit_mm`, found with fast morphology (erosion + dilation).

    The thickness of each region is then measured with an exact distance transform on a small crop.
    """
    grid = grid or ctx.grid
    assert grid is not None
    # "Deep" voxels are at least half the limit from the surface; anything within a cube of the limit's size
    # around a deep voxel is part of something thick enough (a cube, not a ball, so outer corners count).
    deep = ndimage.binary_erosion(grid.solid, _ball(max(0.5, limit_mm / (2 * grid.pitch) - 1e-3)))
    size = max(3, int(math.ceil(limit_mm / grid.pitch)) + 1)
    covered = ndimage.maximum_filter(deep, size=size)
    thin = grid.solid & ~covered
    labels, n = ndimage.label(thin, structure=np.ones((3, 3, 3)))
    out = []
    if n == 0:
        return out
    slices = ndimage.find_objects(labels)
    dist = _capped_distance(grid.solid, limit_mm / (2 * grid.pitch) + 1.5)
    for k, sl in enumerate(slices, 1):   # per-region work on its own crop (scipy's label reductions scan the grid)
        if sl is None:
            continue
        region = labels[sl] == k
        count = int(region.sum())
        if count < 8:
            continue
        d = float(dist[sl][region].max())
        coords = np.argwhere(region) + np.array([s.start for s in sl])
        centre = grid.centre(coords.mean(axis=0))
        extent = (np.array([s.stop - s.start for s in sl])) * grid.pitch
        est = float((2 * float(d) - 0.5) * grid.pitch)
        out.append({"centre": centre, "extent": extent, "thickness": max(grid.pitch, est), "voxels": int(count)})
    return sorted(out, key=lambda c: -c["voxels"])


def check_thin(ctx: Context) -> None:
    mat, printer = ctx.material, ctx.printer
    min_wall = max(mat["min_wall_mm"], 2 * printer["nozzle"])
    seen: set[str] = set()
    for comp in _thin_components(ctx, min_wall)[:12]:
        ext = np.sort(comp["extent"])
        probe = CadFinding("thin_wall", 0, "", "", location=_loc(comp["centre"]))
        key = fingerprint(probe, ctx.mesh.bounds)
        if key in seen or len(seen) >= 6:
            continue
        seen.add(key)
        kind = "pin or rod" if ext[1] < 3 * min_wall else "wall"
        ctx.add(CadFinding("thin_wall", 3 if comp["thickness"] < min_wall * 0.75 else 2,
                           f"Thin {kind}: about {comp['thickness']:.1f} mm",
                           f"A {kind} about {comp['thickness']:.1f} mm thick (minimum about {min_wall:.1f} mm for a "
                           f"{printer['nozzle']} mm nozzle in {mat['name']}). The slicer may drop it or print it "
                           "fragile.", f"Thicken it to at least {min_wall * 1.5:.1f} mm (two or three perimeters).",
                           _loc(comp["centre"], float(ext[-1]) / 2), {"thickness_mm": round(comp["thickness"], 2),
                                                                      "kind": kind}))


def check_slender(ctx: Context) -> None:
    """Slender features standing upright break along their layer lines."""
    # Features up to ~6 mm wide don't need the fine grid used for thin walls.
    for comp in _thin_components(ctx, 5.0, coarse_grid(ctx, 0.8))[:10]:
        ex, ey, ez = comp["extent"]
        if ez >= 3 * max(ex, ey) and ez >= 8 and max(ex, ey) < 6:
            ctx.add(CadFinding("weak_layers", 2, "Upright slender feature is weak across layers",
                               f"A feature about {max(ex, ey):.1f} mm wide and {ez:.0f} mm tall stands upright, so "
                               "every layer line runs across it; bending loads snap it along a layer.",
                               "Print it lying down, make it thicker, or add a fillet where it meets the body.",
                               _loc(comp["centre"], ez / 2), {"width_mm": round(float(max(ex, ey)), 1),
                                                              "height_mm": round(float(ez), 1)}))


# ----------------------------------------------------------------- sharp edges


def check_sharp_edges(ctx: Context) -> None:
    mesh = ctx.mesh
    if len(mesh.face_adjacency) == 0:
        return
    angles = np.degrees(mesh.face_adjacency_angles)
    convex = mesh.face_adjacency_convex
    sharp = convex & (angles > 100)              # interior angle below 80°
    if ctx.exterior is not None:
        pair = mesh.face_adjacency
        sharp &= ctx.exterior[pair[:, 0]] & ctx.exterior[pair[:, 1]]
        edges = mesh.vertices[mesh.face_adjacency_edges]
        mids = edges.mean(axis=1)
        outward = mesh.face_normals[pair[:, 0]] + mesh.face_normals[pair[:, 1]]
        outward /= np.maximum(np.linalg.norm(outward, axis=1, keepdims=True), 1e-9)
        sharp &= ctx.grid.is_outside(mids + outward * ctx.grid.pitch * 1.5) if ctx.grid is not None else sharp
    if not sharp.any():
        return
    edge_vertices = mesh.vertices[mesh.face_adjacency_edges[sharp]]
    lengths = np.linalg.norm(edge_vertices[:, 0] - edge_vertices[:, 1], axis=1)
    mids = edge_vertices.mean(axis=1)
    sharp_angles = angles[sharp]
    handled = bool(ctx.use.get("handheld") or ctx.use.get("wearable"))
    # group nearby sharp edges into one finding each
    radius = max(4.0, float(np.max(mesh.extents)) * 0.05)
    pairs = cKDTree(mids).query_pairs(r=radius, output_type="ndarray")
    graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])) if len(pairs) else ([], ([], [])),
                       shape=(len(mids), len(mids)))
    n, labels = connected_components(graph, directed=False)
    groups = sorted((np.flatnonzero(labels == k) for k in range(n)), key=lambda g: -lengths[g].sum())
    for g in groups[:3]:
        total = float(lengths[g].sum())
        if total < 3.0:
            continue
        centre = np.average(mids[g], axis=0, weights=lengths[g])
        interior = 180 - float(sharp_angles[g].max())
        ctx.add(CadFinding("sharp_edge", 3 if handled else 2, "Knife-like edge",
                           f"{total:.0f} mm of edges meet at about {interior:.0f}° (sharper than 80°). Printed, they "
                           "chip easily" + (" and dig into hands or skin." if handled else "."),
                           "Add a fillet or chamfer of at least 0.5-1 mm.",
                           _loc(centre, float(np.ptp(mids[g], axis=0).max()) / 2 + 2),
                           {"length_mm": round(total, 1), "angle_deg": round(interior, 1)}))


# ------------------------------------------------------------------ clearances


def point_triangle_distance(p: np.ndarray, tri: np.ndarray) -> np.ndarray:
    """Exact distance from one point to many triangles [n, 3, 3] (Ericson, Real-Time Collision Detection)."""
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = np.einsum("ij,ij->i", ab, ap), np.einsum("ij,ij->i", ac, ap)
    bp, cp = p - b, p - c
    d3, d4 = np.einsum("ij,ij->i", ab, bp), np.einsum("ij,ij->i", ac, bp)
    d5, d6 = np.einsum("ij,ij->i", ab, cp), np.einsum("ij,ij->i", ac, cp)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    denom = np.where(np.abs(va + vb + vc) < 1e-12, 1e-12, va + vb + vc)
    v, w = vb / denom, vc / denom
    closest = a + ab * v[:, None] + ac * w[:, None]                       # inside the face
    with np.errstate(divide="ignore", invalid="ignore"):
        t_ab = np.clip(np.nan_to_num(d1 / (d1 - d3)), 0, 1)
        t_ac = np.clip(np.nan_to_num(d2 / (d2 - d6)), 0, 1)
        t_bc = np.clip(np.nan_to_num((d4 - d3) / ((d4 - d3) + (d5 - d6))), 0, 1)
    edge_ab, edge_ac, edge_bc = a + ab * t_ab[:, None], a + ac * t_ac[:, None], b + (c - b) * t_bc[:, None]
    region_a = (d1 <= 0) & (d2 <= 0)
    region_b = (d3 >= 0) & (d4 <= d3)
    region_c = (d6 >= 0) & (d5 <= d6)
    region_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    region_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    region_bc = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
    closest = np.where(region_bc[:, None], edge_bc, closest)
    closest = np.where(region_ac[:, None], edge_ac, closest)
    closest = np.where(region_ab[:, None], edge_ab, closest)
    closest = np.where(region_c[:, None], c, closest)
    closest = np.where(region_b[:, None], b, closest)
    closest = np.where(region_a[:, None], a, closest)
    return np.linalg.norm(p - closest, axis=1)


TOUCH_MM = 0.02  # closer than this counts as touching


def shell_gap(a: trimesh.Trimesh, b: trimesh.Trimesh, samples: int = 20000) -> tuple[float, np.ndarray, int]:
    """Smallest distance between two surfaces (sampled candidates refined with exact point-triangle
    distances) and how many sampled points touch (face contact means the parts are glued together)."""
    pa = a.sample(min(samples, max(2000, int(a.area * 8))))
    pb = b.sample(min(samples, max(2000, int(b.area * 8))))
    dist, _ = cKDTree(pb).query(pa, k=1)
    best, where, touching = float("inf"), pa[0], 0
    centres = cKDTree(b.triangles_center)
    reach = float(np.sqrt(b.area_faces.max())) * 2
    for i in np.argsort(dist)[:200]:
        near = centres.query_ball_point(pa[i], r=float(dist[i]) + reach)
        if not near:
            continue
        d = float(point_triangle_distance(pa[i], b.triangles[near]).min())
        touching += int(d < TOUCH_MM)
        if d < best:
            best, where = d, pa[i]
    return best, where, touching


def check_clearances(ctx: Context) -> None:
    shells = ctx.model.shells()
    if len(shells) < 2 or ctx.grid is None or len(shells) > 40:
        return
    clearance = ctx.material["clearance_mm"]
    pitch = ctx.grid.pitch
    vox = [voxelize(s, pitch, align_to=ctx.grid.origin) for s in shells]

    def overlap(i: int, j: int) -> bool:
        gi, gj = vox[i], vox[j]
        oi, oj = gi.offset_in(ctx.grid), gj.offset_in(ctx.grid)
        lo = np.maximum(oi, oj)
        hi = np.minimum(oi + np.array(gi.solid.shape), oj + np.array(gj.solid.shape))
        if np.any(hi <= lo):
            return False
        si = gi.solid[tuple(slice(l - o, h - o) for l, h, o in zip(lo, hi, oi))]
        sj = gj.solid[tuple(slice(l - o, h - o) for l, h, o in zip(lo, hi, oj))]
        return bool((si & sj).sum() > 2)

    reported = 0
    for i in range(len(shells)):
        for j in range(i + 1, len(shells)):
            lo = np.maximum(shells[i].bounds[0], shells[j].bounds[0]) - clearance - 1
            hi = np.minimum(shells[i].bounds[1], shells[j].bounds[1]) + clearance + 1
            if np.any(lo > hi) or overlap(i, j):
                continue  # far apart, or overlapping on purpose (they print as one solid)
            gap, where, touching = shell_gap(shells[i], shells[j])
            if touching >= 5:
                continue  # faces in contact: the slicer prints them as one solid
            if gap < TOUCH_MM and reported < 4:
                ctx.add(CadFinding("weak_joint", 3, "Parts only touch along an edge",
                                   "Two bodies meet only at an edge or a point, not a face. The joint is a single "
                                   "line of plastic and will snap off (or the slicer may print them apart).",
                                   "Overlap the bodies by at least 0.5 mm, or merge them in CAD.", _loc(where, 3.0)))
                reported += 1
            elif gap < clearance and reported < 4:
                ctx.add(CadFinding("clearance", 3, f"Parts only {gap:.2f} mm apart",
                                   f"Two separate parts come within about {gap:.2f} mm of each other (about "
                                   f"{clearance} mm is needed in {ctx.material['name']}). Printed together they will "
                                   "fuse; printed apart they will be too tight to fit.",
                                   f"Open the gap to at least {clearance} mm (measure your printer: see `physical "
                                   "printed`).", _loc(where, 3.0), {"gap_mm": round(gap, 3)}))
                reported += 1


# ------------------------------------------------------------------- stability


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


# ------------------------------------------------------------------- estimates


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


# ------------------------------------------------------------------------ run


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
                  check_slender, check_sharp_edges, check_clearances, check_stability):
        try:
            check(ctx)
        except Exception as exc:  # one failing check must not hide the others
            log.warning("check %s failed: %s", check.__name__, exc)
            ctx.add(CadFinding("check_error", 1, f"Could not run {check.__name__}", str(exc)))
    estimates(ctx)
    ctx.findings.sort(key=lambda f: -f.severity)
    return ctx
