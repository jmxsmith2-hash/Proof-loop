"""Gaps between separate bodies: too tight to move, fused, or only touching at an edge."""

from __future__ import annotations

import numpy as np
import trimesh
from scipy.spatial import cKDTree

from proofloop.check.cad.context import CadFinding, Context, _loc
from proofloop.check.cad.voxel import voxelize


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
    grid = ctx.grid
    clearance = ctx.material["clearance_mm"]
    pitch = grid.pitch
    vox = [voxelize(s, pitch, align_to=grid.origin) for s in shells]

    def overlap(i: int, j: int) -> bool:
        gi, gj = vox[i], vox[j]
        oi, oj = gi.offset_in(grid), gj.offset_in(grid)
        lo = np.maximum(oi, oj)
        hi = np.minimum(oi + np.array(gi.solid.shape), oj + np.array(gj.solid.shape))
        if np.any(hi <= lo):
            return False
        si = gi.solid[tuple(slice(a - o, b - o) for a, b, o in zip(lo, hi, oi, strict=False))]
        sj = gj.solid[tuple(slice(a - o, b - o) for a, b, o in zip(lo, hi, oj, strict=False))]
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
