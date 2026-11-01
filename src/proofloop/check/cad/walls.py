"""Thin walls, slender features and flexing tabs: checks on how thick things are."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy import ndimage

from proofloop.check.cad.context import CadFinding, Context, _loc, fingerprint
from proofloop.check.cad.voxel import VoxelGrid


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


def _thin_components(ctx: Context, limit_mm: float, grid: VoxelGrid | None = None,
                     with_voxels: bool = False) -> list[dict[str, Any]]:
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
    out: list[dict[str, Any]] = []
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
        item = {"centre": centre, "extent": extent, "thickness": max(grid.pitch, est), "voxels": int(count)}
        if with_voxels:
            item["index"] = coords
        out.append(item)
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
    tabs = ctx.cache.get("cantilevers", [])
    for comp in _thin_components(ctx, 5.0, coarse_grid(ctx, 0.8))[:10]:
        ex, ey, ez = comp["extent"]
        if any(np.all(comp["centre"] >= lo) and np.all(comp["centre"] <= hi) for lo, hi in
               (t["box"] for t in tabs)):
            continue                                     # already reported as a flexing tab
        if ez >= 3 * max(ex, ey) and ez >= 8 and max(ex, ey) < 6:
            ctx.add(CadFinding("weak_layers", 2, "Upright slender feature is weak across layers",
                               f"A feature about {max(ex, ey):.1f} mm wide and {ez:.0f} mm tall stands upright, so "
                               "every layer line runs across it; bending loads snap it along a layer.",
                               "Print it lying down, make it thicker, or add a fillet where it meets the body.",
                               _loc(comp["centre"], ez / 2), {"width_mm": round(float(max(ex, ey)), 1),
                                                              "height_mm": round(float(ez), 1)}))


def check_cantilevers(ctx: Context) -> None:
    """Thin tabs fixed at one end (clips, snap-fits, latches, spring fingers): how far they bend safely."""
    grid, mat = ctx.grid, ctx.material
    if grid is None:
        return
    min_wall = max(mat["min_wall_mm"], 2 * ctx.printer["nozzle"])
    strain = float(mat.get("snap_strain_pct", 2.0)) / 100
    found = []
    g = coarse_grid(ctx, max(0.5, 2 * grid.pitch))   # candidates on a coarser grid; thickness measured finely below
    for comp in _thin_components(ctx, 3.5, g, with_voxels=True)[:12]:
        if comp["voxels"] < 12:
            continue
        idx = comp["index"]
        t = _fine_thickness(grid, g, idx) or comp["thickness"]
        if t < min_wall * 0.9:
            continue                                     # thinner than a wall: check_thin reports it
        pts = g.centre(idx)
        centred = pts - pts.mean(axis=0)
        _, _, vt = np.linalg.svd(centred, full_matrices=False)
        axis = vt[0]
        along = centred @ axis
        length = float(np.ptp(along)) + g.pitch
        width = float(np.ptp(centred @ vt[1])) + g.pitch
        if length < 6 or length < 5 * t or width < 1.5 * t or width > max(25.0, length / 2):
            continue                                     # pins (slender check) and wide plates are not tabs
        region = np.zeros(g.solid.shape, bool)
        region[tuple(idx.T)] = True
        lo = np.maximum(idx.min(axis=0) - 2, 0)
        hi = np.minimum(idx.max(axis=0) + 3, g.solid.shape)
        box = tuple(slice(a, b) for a, b in zip(lo, hi, strict=False))
        touch = ndimage.binary_dilation(region[box], np.ones((3, 3, 3))) & g.solid[box] & ~region[box]
        if not touch.any():
            continue                                     # the whole part is thin: a plate, not a tab
        contact = (g.centre(np.argwhere(touch) + lo) - pts.mean(axis=0)) @ axis
        span = float(np.ptp(contact)) + g.pitch
        lo_end, hi_end = along.min(), along.max()
        at_end = contact.max() < lo_end + 0.3 * length or contact.min() > hi_end - 0.3 * length
        if span > 0.35 * length or not at_end:
            continue                                     # held along its length or at both ends: a wall or a bridge
        upright = abs(axis[2]) > 0.7
        deflect = strain * length ** 2 / (1.5 * t)
        found.append({"centre": comp["centre"], "box": (pts.min(axis=0) - 1, pts.max(axis=0) + 1)})
        detail = (f"A tab about {length:.0f} mm long, {width:.0f} mm wide and {t:.1f} mm thick is fixed at one end, like a "
                  f"clip or snap-fit. In {mat['name']} its free end can bend about {deflect:.1f} mm before it is "
                  f"strained past about {strain:.0%} (where it cracks or stays bent).")
        if upright:
            ctx.add(CadFinding("cantilever", 3, "Flexing tab printed upright snaps along its layers",
                               detail + " It stands upright, so bending pulls the layers apart: it will snap at the root.",
                               "Print it lying flat (layers along the tab), give it a fillet where it joins, "
                               "or use PETG or ABS, which flex further.", _loc(comp["centre"], length / 2),
                               {"length_mm": round(length, 1), "thickness_mm": round(t, 2), "safe_deflection_mm":
                                round(deflect, 2), "upright": True}))
        else:
            ctx.add(CadFinding("cantilever", 1, f"Flexing tab: bends about {deflect:.1f} mm safely",
                               detail, f"For more travel make it longer or thinner at the root, or taper it "
                               f"(thickest at the root); a snap-fit hook should need less than {deflect:.1f} mm.",
                               _loc(comp["centre"], length / 2),
                               {"length_mm": round(length, 1), "thickness_mm": round(t, 2), "safe_deflection_mm":
                                round(deflect, 2), "upright": False}))
        if len(found) >= 4:
            break
    ctx.cache["cantilevers"] = found


def _fine_thickness(fine: VoxelGrid, coarse: VoxelGrid, idx: np.ndarray) -> float | None:
    """Thickness of a region found on a coarse grid, measured on the fine grid (twice the deepest point)."""
    k = max(1, int(round(coarse.pitch / fine.pitch)))
    lo = np.maximum(idx.min(axis=0) * k - 2, 0)
    hi = np.minimum((idx.max(axis=0) + 1) * k + 2, fine.solid.shape)
    crop = fine.solid[tuple(slice(a, b) for a, b in zip(lo, hi, strict=False))]
    if not crop.any():
        return None
    mask = np.zeros(crop.shape, bool)
    for off in np.ndindex(k, k, k):                   # the fine voxels inside the region's coarse voxels
        cells = idx * k + np.array(off) - lo
        ok = np.all((cells >= 0) & (cells < crop.shape), axis=1)
        mask[tuple(cells[ok].T)] = True
    dist = ndimage.distance_transform_edt(np.pad(crop, 1))[1:-1, 1:-1, 1:-1]
    inside = dist[mask & crop]
    return float((2 * inside.max() - 0.5) * fine.pitch) if inside.size else None
