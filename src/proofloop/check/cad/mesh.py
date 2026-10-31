"""Mesh health, build volume and sharp edges: checks on the surface itself."""

from __future__ import annotations

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from proofloop.check.cad.context import CadFinding, Context, _loc


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
