"""Fine detail a printer can't reproduce: lines narrower than the nozzle, grooves that fill in,
and raised or sunk steps thinner than a layer.

Cross-sections are cut from the mesh at a few heights and drawn at about a sixth of the nozzle
width (scanline fill by winding number, like a slicer's first step). A morphological opening with a disc
the size of the nozzle removes everything a nozzle-wide line can't trace; a closing fills every
gap it can't leave open. What is lost is what the slicer will drop or blur: thin text strokes,
fine ridges, engraved lines.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np
import trimesh
from scipy import ndimage

MAX_SLICE_PIXELS = 2_500_000
DETAIL_SLICES = 24


def section_mask(mesh: trimesh.Trimesh, z: float, origin: np.ndarray, pitch: float, shape: tuple[int, int],
                 with_edges: bool = False):
    """Inside/outside raster of the cross-section at height z (rows = y, columns = x).

    Uses the winding number, not even-odd: scanning in +x, a face whose outward normal points to -x
    is entered (+1), one pointing to +x is left (-1). Overlapping or touching bodies and inverted
    inner shells (modelled cavities) then come out right without merging the mesh first.
    """
    segs, faces = trimesh.intersections.mesh_plane(mesh, plane_normal=[0, 0, 1], plane_origin=[0, 0, z],
                                                   return_faces=True)
    h, w = shape
    if len(segs) == 0:
        return (np.zeros(shape, bool), segs, faces) if with_edges else np.zeros(shape, bool)
    sign = np.where(mesh.face_normals[faces, 0] < 0, 1, -1)
    xa, ya, xb, yb = segs[:, 0, 0], segs[:, 0, 1], segs[:, 1, 0], segs[:, 1, 1]
    swap = ya > yb
    xa, xb = np.where(swap, xb, xa), np.where(swap, xa, xb)
    ya, yb = np.where(swap, yb, ya), np.where(swap, ya, yb)
    r0 = np.ceil((ya - origin[1]) / pitch - 0.5).astype(int)
    r1 = np.ceil((yb - origin[1]) / pitch - 0.5).astype(int)           # rows r0 .. r1-1 are crossed
    r0, r1 = np.clip(r0, 0, h), np.clip(r1, 0, h)
    n = np.maximum(r1 - r0, 0)
    keep = n > 0
    if not keep.any():
        return (np.zeros(shape, bool), segs, faces) if with_edges else np.zeros(shape, bool)
    seg = np.repeat(np.flatnonzero(keep), n[keep])
    rows = r0[seg] + (np.arange(len(seg)) - np.repeat(np.cumsum(n[keep]) - n[keep], n[keep]))
    yc = origin[1] + (rows + 0.5) * pitch
    t = (yc - ya[seg]) / (yb[seg] - ya[seg])
    xc = xa[seg] + t * (xb[seg] - xa[seg])
    cols = np.clip(np.ceil((xc - origin[0]) / pitch - 0.5).astype(int), 0, w)
    winding = np.zeros((h, w + 1), np.int32)
    np.add.at(winding, (rows, cols), sign[seg])
    inside = np.cumsum(winding, axis=1)[:, :w] > 0
    return (inside, segs, faces) if with_edges else inside


def _morph(mask: np.ndarray, disc: np.ndarray, op: str) -> np.ndarray:
    """Binary opening or closing; OpenCV when it is available (much faster), else scipy."""
    try:
        import cv2

        kind = cv2.MORPH_OPEN if op == "open" else cv2.MORPH_CLOSE
        pad = disc.shape[0]
        img = np.pad(mask.astype(np.uint8), pad)          # zero border, like scipy's default
        out = cv2.morphologyEx(img, kind, disc.astype(np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0)
        return out[pad:-pad, pad:-pad].astype(bool)
    except ImportError:
        fn = ndimage.binary_opening if op == "open" else ndimage.binary_closing
        return fn(mask, disc)


def _disc(radius_px: float) -> np.ndarray:
    r = int(math.ceil(radius_px))
    ax = np.arange(-r, r + 1)
    return (ax[:, None] ** 2 + ax[None, :] ** 2) <= radius_px ** 2 + 1e-9


def slice_heights(mesh: trimesh.Trimesh, layer: float, n: int = DETAIL_SLICES) -> list[float]:
    """Evenly spread heights plus one just under each large top surface (where embossed text sits)."""
    lo, hi = float(mesh.bounds[0, 2]), float(mesh.bounds[1, 2])
    zs = list(np.linspace(lo + layer / 2, hi - layer / 2, max(2, n)))
    normals, areas, cz = mesh.face_normals, mesh.area_faces, mesh.triangles_center[:, 2]
    up = normals[:, 2] > 0.999
    if up.any():
        levels: dict[float, float] = {}
        for z, a in zip(np.round(cz[up], 2), areas[up], strict=False):
            levels[float(z)] = levels.get(float(z), 0.0) + float(a)
        for z, _a in sorted(levels.items(), key=lambda kv: -kv[1])[:6]:
            if lo + layer < z < hi + 1e-6:
                zs.append(z + layer * 0.5)        # just above a surface: what's embossed on it
                zs.append(z - layer * 0.5)        # just below: what's engraved into it
    # a hair off the modelled heights: a cut exactly through a flat face is ambiguous
    return sorted({round(z, 3) + 1e-4 for z in zs if lo < z < hi})


def lost_detail(mesh: trimesh.Trimesh, nozzle: float, layer: float,
                one_body: Callable[[list[set[int]]], bool] | None = None,
                face_shell: np.ndarray | None = None) -> dict[str, list[dict[str, Any]]]:
    """Regions too narrow to print (raised) and gaps too narrow to stay open (engraved), per slice.

    A narrow gap between pieces of a cut that belong to separate bodies is a clearance between parts
    (checked elsewhere), not an engraving. `face_shell` gives each face's shell; `one_body(sets)` says
    whether shells from each set join up into one solid body.
    """
    (x0, y0, _), (x1, y1, _) = mesh.bounds
    pitch = nozzle / 6
    margin = nozzle * 2
    w, h = int((x1 - x0 + 2 * margin) / pitch) + 1, int((y1 - y0 + 2 * margin) / pitch) + 1
    if w * h > MAX_SLICE_PIXELS:
        pitch = math.sqrt((x1 - x0 + 2 * margin) * (y1 - y0 + 2 * margin) / MAX_SLICE_PIXELS)
        w, h = int((x1 - x0 + 2 * margin) / pitch) + 1, int((y1 - y0 + 2 * margin) / pitch) + 1
    if pitch > nozzle / 3:
        return {"thin": [], "gaps": [], "skipped": ["the part is large, so fine detail was checked coarsely"]}
    origin = np.array([x0 - margin, y0 - margin])
    disc = _disc(nozzle / 2 / pitch)
    min_area = (0.8 * nozzle) ** 2                    # ignore corner slivers left by the round tool
    cache: dict[float, dict[str, np.ndarray]] = {}

    analysed: dict[bytes, dict[str, np.ndarray]] = {}

    def cut(z: float) -> dict[str, Any]:
        if z not in cache:
            inside, segs, faces = section_mask(mesh, z, origin, pitch, (h, w), with_edges=True)
            key = np.packbits(inside).tobytes()
            if key not in analysed:                     # straight walls give the same cut at many heights
                analysed[key] = {"thin": inside & ~_morph(inside, disc, "open"),
                                 "gaps": _morph(inside, disc, "close") & ~inside, "repeat": False}
            else:
                analysed[key] = {**analysed[key], "repeat": True}
            cache[z] = {"inside": inside, **analysed[key], "segs": segs, "faces": faces}
        return cache[z]

    def persists(kind: str, z: float, region: np.ndarray, box: tuple[slice, slice]) -> bool:
        """A real detail has straight sides: half a layer up or down it is still there and still narrow.
        The thin edge where a sloping face meets a flat one is not (it widens or vanishes)."""
        for dz in (layer / 2, -layer / 2):
            if not lo_z < z + dz < hi_z:
                continue
            other = cut(round(z + dz, 4))[kind][box]
            if (other & region).sum() >= 0.6 * region.sum():
                return True
        return False

    lo_z, hi_z = float(mesh.bounds[0, 2]), float(mesh.bounds[1, 2])
    thin_out, gap_out = [], []
    for z in slice_heights(mesh, layer):
        c = cut(z)
        inside = c["inside"]
        if inside.sum() < 50 or c["repeat"]:
            continue                                    # nothing there, or the same cut as a lower slice
        parts, _ = ndimage.label(inside, structure=np.ones((3, 3)))
        for kind in ("thin", "gaps"):
            mask = c[kind]
            labels, n = ndimage.label(mask, structure=np.ones((3, 3)))
            if n == 0:
                continue
            sizes = ndimage.sum_labels(mask, labels, np.arange(1, n + 1)) * pitch ** 2
            boxes = ndimage.find_objects(labels)
            for k in np.flatnonzero(sizes >= min_area) + 1:
                sl = boxes[k - 1]
                big = tuple(slice(max(0, s.start - 3), s.stop + 3) for s in sl)
                region = labels[big] == k
                if not persists(kind, z, region, big):
                    continue
                if kind == "gaps":
                    grown = ndimage.binary_dilation(region, np.ones((3, 3)), iterations=2)
                    touching = np.unique(parts[big][grown])
                    touching = touching[touching > 0]
                    if len(touching) >= 2 and one_body is not None and face_shell is not None:
                        shells = _piece_shells(c, parts, touching, origin, pitch, face_shell)
                        if not one_body(shells):
                            continue              # between two separate bodies: a clearance, checked elsewhere
                width = 2 * float(ndimage.distance_transform_edt(region).max()) * pitch
                ys, xs = np.nonzero(region)
                cx = origin[0] + (xs.mean() + big[1].start + 0.5) * pitch
                cy = origin[1] + (ys.mean() + big[0].start + 0.5) * pitch
                ext = max(sl[0].stop - sl[0].start, sl[1].stop - sl[1].start) * pitch
                (thin_out if kind == "thin" else gap_out).append(
                    {"z": z, "x": float(cx), "y": float(cy), "area": float(sizes[k - 1]), "width": width, "extent": ext})
    return {"thin": thin_out, "gaps": gap_out, "skipped": []}


def _piece_shells(cut: dict[str, Any], parts: np.ndarray, labels: np.ndarray, origin: np.ndarray, pitch: float,
                  face_shell: np.ndarray) -> list[set[int]]:
    """For each piece of a cut, the mesh shells whose faces form its outline."""
    segs, faces = cut["segs"], cut["faces"]
    h, w = parts.shape
    mid = segs.mean(axis=1)[:, :2]
    out = []
    for lab in labels:
        shells: set[int] = set()
        cols = np.floor((mid[:, 0] - origin[0]) / pitch).astype(int)
        rows = np.floor((mid[:, 1] - origin[1]) / pitch).astype(int)
        hit = np.zeros(len(mid), bool)
        for dr in (-1, 0, 1):                         # the outline runs along the piece's edge pixels
            for dc in (-1, 0, 1):
                r, cc = np.clip(rows + dr, 0, h - 1), np.clip(cols + dc, 0, w - 1)
                hit |= parts[r, cc] == lab
        shells.update(int(x) for x in np.unique(face_shell[faces[hit]]))
        out.append(shells)
    return out


def thin_steps(mesh: trimesh.Trimesh, layer: float, min_area_mm2: float = 1.0) -> list[dict[str, Any]]:
    """Flat surfaces closer together in height than about a layer: raised or sunk detail that is too shallow."""
    normals, areas, cz = mesh.face_normals, mesh.area_faces, mesh.triangles_center[:, 2]
    flat = np.abs(normals[:, 2]) > 0.999
    if not flat.any():
        return []
    up = normals[:, 2] > 0
    out = []
    for facing in (True, False):
        sel = flat & (up == facing)
        if not sel.any():
            continue
        levels: dict[float, list[int]] = {}
        for i, z in zip(np.flatnonzero(sel), np.round(cz[sel], 3), strict=False):
            levels.setdefault(float(z), []).append(int(i))
        zs = sorted(z for z, idx in levels.items() if areas[idx].sum() >= min_area_mm2)
        for a, b in zip(zs, zs[1:], strict=False):
            step = b - a
            if not 0.005 < step < 0.75 * layer:
                continue
            fa, fb = levels[a], levels[b]
            ba = mesh.triangles[fa].reshape(-1, 3)
            bb = mesh.triangles[fb].reshape(-1, 3)
            lo = np.maximum(ba[:, :2].min(0), bb[:, :2].min(0))
            hi = np.minimum(ba[:, :2].max(0), bb[:, :2].max(0))
            if np.any(hi < lo):
                continue                              # surfaces in different places: not one detail
            smaller = fa if areas[fa].sum() < areas[fb].sum() else fb
            centre = mesh.triangles_center[smaller].mean(axis=0)
            out.append({"step": float(step), "z": float(b), "facing": "up" if facing else "down",
                        "area": float(min(areas[fa].sum(), areas[fb].sum())), "centre": centre})
    return out
