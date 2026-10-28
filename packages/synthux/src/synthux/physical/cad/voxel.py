"""Solid voxelization by signed winding count along vertical columns.

Each triangle that covers a column centre adds an event at its height: +1 when
the surface faces down (the column enters the solid), -1 when it faces up. A
voxel is solid where the running count is positive. Overlapping shells, which
CAD exports often contain, are therefore treated as one solid instead of
cancelling out the way plain even/odd filling would.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import trimesh

MAX_PAIRS_PER_CHUNK = 4_000_000


@dataclass
class VoxelGrid:
    solid: np.ndarray        # bool [nx, ny, nz]
    origin: np.ndarray       # world position of voxel (0, 0, 0)'s corner
    pitch: float

    def centre(self, idx: np.ndarray) -> np.ndarray:
        """World coordinates (mm) of voxel centres for index rows [i, j, k]."""
        return self.origin + (np.asarray(idx, dtype=float) + 0.5) * self.pitch

    def offset_in(self, other: "VoxelGrid") -> np.ndarray:
        """Integer index offset of this grid inside another grid with the same pitch."""
        return np.round((self.origin - other.origin) / self.pitch).astype(int)

    def is_outside(self, points: np.ndarray) -> np.ndarray:
        """True where a point falls in an empty voxel (or outside the grid)."""
        idx = np.floor((np.asarray(points) - self.origin) / self.pitch).astype(int)
        inside_grid = np.all((idx >= 0) & (idx < np.array(self.solid.shape)), axis=1)
        out = np.ones(len(idx), dtype=bool)
        ok = idx[inside_grid]
        out[inside_grid] = ~self.solid[ok[:, 0], ok[:, 1], ok[:, 2]]
        return out

    @property
    def volume_mm3(self) -> float:
        return float(self.solid.sum()) * self.pitch ** 3


def choose_pitch(extents: np.ndarray, wanted: float, max_cells: int = 16_000_000) -> float:
    """The finest pitch <= wanted that keeps the grid under max_cells."""
    padded = np.asarray(extents, dtype=float) + 2 * wanted
    pitch = wanted
    while np.prod(np.ceil(padded / pitch) + 2) > max_cells:
        pitch *= 1.15
    return round(pitch, 4)


def voxelize(mesh: trimesh.Trimesh, pitch: float, align_to: np.ndarray | None = None) -> VoxelGrid:
    """Solid voxels of the mesh. `align_to` snaps the grid to another grid's origin (for comparing parts)."""
    lo, hi = mesh.bounds
    origin = lo - pitch
    if align_to is not None:
        origin = align_to + np.floor((origin - align_to) / pitch) * pitch
    dims = np.ceil((hi - origin) / pitch).astype(int) + 2
    nx, ny, nz = (int(d) for d in dims)
    tri = mesh.triangles  # [n, 3, 3]
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    nz_sign = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    keep = np.abs(nz_sign) > 1e-12
    a, b, c, nz_sign = a[keep], b[keep], c[keep], nz_sign[keep]
    # a tiny irrational offset keeps column centres off shared edges and vertices
    jitter = pitch * np.array([1.3e-4 * math.pi, 1.7e-4 * math.e])
    xs_min = np.minimum(np.minimum(a[:, 0], b[:, 0]), c[:, 0])
    xs_max = np.maximum(np.maximum(a[:, 0], b[:, 0]), c[:, 0])
    ys_min = np.minimum(np.minimum(a[:, 1], b[:, 1]), c[:, 1])
    ys_max = np.maximum(np.maximum(a[:, 1], b[:, 1]), c[:, 1])
    i0 = np.ceil((xs_min - origin[0] - jitter[0]) / pitch - 0.5).astype(int)
    i1 = np.floor((xs_max - origin[0] - jitter[0]) / pitch - 0.5).astype(int)
    j0 = np.ceil((ys_min - origin[1] - jitter[1]) / pitch - 0.5).astype(int)
    j1 = np.floor((ys_max - origin[1] - jitter[1]) / pitch - 0.5).astype(int)
    ni = np.clip(i1 - i0 + 1, 0, None)
    nj = np.clip(j1 - j0 + 1, 0, None)
    counts = ni * nj
    diff = np.zeros(nx * ny * (nz + 1), dtype=np.int32)

    cols_all, z_all, s_all = [], [], []
    start = 0
    cumulative = np.cumsum(counts)
    while start < len(counts):
        base = cumulative[start - 1] if start else 0
        end = int(np.searchsorted(cumulative, base + MAX_PAIRS_PER_CHUNK, side="right"))
        end = max(end, start + 1)
        sl = slice(start, end)
        cnt = counts[sl]
        total = int(cnt.sum())
        if total:
            t = np.repeat(np.arange(start, end), cnt)
            local = np.arange(total) - np.repeat(np.cumsum(cnt) - cnt, cnt)
            nj_t = nj[t]
            ii = i0[t] + local // np.maximum(nj_t, 1)
            jj = j0[t] + local % np.maximum(nj_t, 1)
            px = origin[0] + (ii + 0.5) * pitch + jitter[0]
            py = origin[1] + (jj + 0.5) * pitch + jitter[1]
            ax, ay, az = a[t, 0], a[t, 1], a[t, 2]
            bx, by, bz = b[t, 0], b[t, 1], b[t, 2]
            cx, cy, cz = c[t, 0], c[t, 1], c[t, 2]
            area = nz_sign[t]
            w0 = ((bx - px) * (cy - py) - (by - py) * (cx - px)) / area
            w1 = ((cx - px) * (ay - py) - (cy - py) * (ax - px)) / area
            w2 = 1.0 - w0 - w1
            inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            if inside.any():
                z = w0[inside] * az[inside] + w1[inside] * bz[inside] + w2[inside] * cz[inside]
                cols_all.append(ii[inside] * ny + jj[inside])
                z_all.append(z)
                s_all.append(np.where(area[inside] < 0, 1, -1).astype(np.int8))  # down-facing = entering
        start = end

    if cols_all:
        cols = np.concatenate(cols_all)
        zs = np.concatenate(z_all)
        signs = np.concatenate(s_all).astype(np.int32)
        order = np.lexsort((zs, cols))
        cols, zs, signs = cols[order], zs[order], signs[order]
        winding = np.cumsum(signs)
        col_start = np.r_[True, cols[1:] != cols[:-1]]
        start_idx = np.maximum.accumulate(np.where(col_start, np.arange(len(cols)), 0))
        before = np.where(start_idx > 0, winding[start_idx - 1], 0)
        winding = winding - before
        same_col_next = np.r_[cols[1:] == cols[:-1], False]
        fill = (winding > 0) & same_col_next
        z0 = zs[fill]
        z1 = zs[np.flatnonzero(fill) + 1]
        k0 = np.clip(np.ceil((z0 - origin[2]) / pitch - 0.5).astype(int), 0, nz)
        k1 = np.clip(np.ceil((z1 - origin[2]) / pitch - 0.5).astype(int), 0, nz)
        good = k1 > k0
        col = cols[fill][good]
        np.add.at(diff, col * (nz + 1) + k0[good], 1)
        np.add.at(diff, col * (nz + 1) + k1[good], -1)
    solid = np.cumsum(diff.reshape(nx * ny, nz + 1), axis=1)[:, :nz] > 0
    return VoxelGrid(solid=solid.reshape(nx, ny, nz), origin=origin, pitch=pitch)
