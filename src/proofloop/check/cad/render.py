"""Shaded views of a mesh with numbered markers where problems are (pure numpy + Pillow)."""

from __future__ import annotations

import io
import math

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont

VIEWS = {  # (azimuth, elevation) in degrees; camera looks at the model centre
    "iso": (-35.0, 25.0),
    "front": (0.0, 0.0),
    "side": (90.0, 0.0),
    "top": (0.0, 89.9),
    "below": (-35.0, -35.0),
}
SEVERITY_COLOURS = {4: (200, 30, 30), 3: (225, 80, 30), 2: (215, 150, 0), 1: (90, 120, 160)}


def _view_matrix(azimuth: float, elevation: float) -> np.ndarray:
    az, el = math.radians(azimuth), math.radians(elevation)
    rz = np.array([[math.cos(az), -math.sin(az), 0], [math.sin(az), math.cos(az), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, math.cos(el), -math.sin(el)], [0, math.sin(el), math.cos(el)]])
    # world -> camera: x right, y up (screen), z towards the viewer (depth)
    base = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=float)
    return rx @ base @ rz


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render(mesh: trimesh.Trimesh, view: str = "iso", markers: list[dict] | None = None,
           size: tuple[int, int] = (640, 480), highlight: np.ndarray | None = None) -> bytes:
    """markers: [{"x","y","z","r","label","severity"}]; highlight: optional face mask tinted red."""
    w, h = size
    if highlight is None:
        mesh = render_proxy(mesh)
    rot = _view_matrix(*VIEWS[view])
    centre = mesh.bounds.mean(axis=0)
    verts = (mesh.vertices - centre) @ rot.T
    faces = mesh.faces
    tri = verts[faces]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    norm = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = normals / np.maximum(norm, 1e-12)
    facing = normals[:, 2] > 0
    light = np.array([-0.4, 0.55, 0.75])  # from the camera's upper left
    light /= np.linalg.norm(light)
    shade = 0.35 + 0.65 * np.clip(normals @ light, 0, 1)
    lo, hi = verts[:, :2].min(axis=0), verts[:, :2].max(axis=0)
    scale = 0.86 * min(w / max(hi[0] - lo[0], 1e-6), h / max(hi[1] - lo[1], 1e-6))
    offset = np.array([w / 2, h / 2]) - scale * (lo + hi) / 2

    def to_px(p: np.ndarray) -> np.ndarray:
        xy = p[..., :2] * scale + offset
        return np.stack([xy[..., 0], h - xy[..., 1]], axis=-1)

    base = np.array([150, 170, 195], dtype=float)
    hot = np.array([230, 90, 70], dtype=float)
    colours = np.where((highlight[:, None] if highlight is not None else np.zeros((len(faces), 1), bool)), hot, base)
    colours = np.clip(colours * shade[:, None], 0, 255).astype(np.uint8)
    pixels = np.full((h * w, 3), (248, 248, 246), dtype=np.uint8)
    keep = np.flatnonzero(facing)
    _rasterize(to_px(tri[keep]), tri[keep][:, :, 2], colours[keep], w, h, pixels)
    img = Image.fromarray(pixels.reshape(h, w, 3), "RGB")
    draw = ImageDraw.Draw(img)
    font = _font(15)
    for m in markers or []:
        p = to_px(((np.array([m["x"], m["y"], m["z"]]) - centre) @ rot.T)[None])[0]
        r = max(9.0, float(m.get("r", 4.0)) * scale * 0.6)
        r = min(r, 60.0)
        colour = SEVERITY_COLOURS.get(int(m.get("severity", 2)), (215, 150, 0))
        draw.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], outline=colour, width=3)
        label = str(m.get("label", ""))
        if label:
            tx, ty = p[0] + r * 0.75, p[1] - r * 0.75 - 18
            draw.rounded_rectangle([tx - 2, ty - 1, tx + 12 + 8 * len(label), ty + 19], 5, fill=colour)
            draw.text((tx + 4, ty + 1), label, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


PROXY_FACES = 120_000   # more triangles than this are finer than the pixels of a report image


def render_proxy(mesh: trimesh.Trimesh, max_faces: int = PROXY_FACES) -> trimesh.Trimesh:
    """A lighter copy of a very dense mesh for drawing only (vertices merged on a fine grid; cached).

    Checks always use the full mesh; this only affects how the pictures are drawn.
    """
    if len(mesh.faces) <= max_faces:
        return mesh
    cached = mesh.metadata.get("_render_proxy")
    if cached is not None:
        return cached
    cell = float(mesh.extents.max()) / 1200          # about half a pixel at report sizes
    verts, faces = mesh.vertices, mesh.faces
    proxy = mesh
    for _ in range(8):
        keys = np.floor((verts - verts.min(axis=0)) / cell).astype(np.int64)
        _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
        f = inverse.reshape(-1)[faces]
        ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
        f = np.unique(np.sort(f[ok], axis=1), axis=0, return_index=True)[1]
        kept = faces[ok][np.sort(f)]
        proxy = trimesh.Trimesh(verts[first], inverse.reshape(-1)[kept], process=False)
        if len(proxy.faces) <= max_faces:
            break
        cell *= 1.6
    mesh.metadata["_render_proxy"] = proxy
    return proxy


def _rasterize(px: np.ndarray, depth: np.ndarray, colours: np.ndarray, w: int, h: int, out: np.ndarray,
               chunk: int = 6_000_000) -> None:
    """Fill `out` (h*w x 3) with the nearest triangle at each pixel centre (vectorised z-buffer).

    px: [n, 3, 2] pixel coordinates, depth: [n, 3] (larger = nearer), colours: [n, 3].
    """
    if len(px) == 0:
        return
    x0 = np.clip(np.floor(px[:, :, 0].min(axis=1) - 0.5), 0, w - 1).astype(np.int64)
    x1 = np.clip(np.ceil(px[:, :, 0].max(axis=1) - 0.5), 0, w - 1).astype(np.int64)
    y0 = np.clip(np.floor(px[:, :, 1].min(axis=1) - 0.5), 0, h - 1).astype(np.int64)
    y1 = np.clip(np.ceil(px[:, :, 1].max(axis=1) - 0.5), 0, h - 1).astype(np.int64)
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    counts = np.where((bw > 0) & (bh > 0), bw * bh, 0)
    a, b, c = px[:, 0], px[:, 1], px[:, 2]
    area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    usable = np.abs(area) > 1e-9
    zbuf = np.full(h * w, -np.inf)
    ends = np.cumsum(counts)
    start = 0
    while start < len(px):  # chunks of triangles whose candidate pixels fit in memory
        stop = int(np.searchsorted(ends, (ends[start - 1] if start else 0) + chunk, side="right"))
        stop = max(stop, start + 1)
        idx = np.arange(start, stop)
        idx = idx[usable[idx] & (counts[idx] > 0)]
        start = stop
        if not len(idx):
            continue
        n = counts[idx]
        tri_of = np.repeat(idx, n)
        local = np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
        xs = x0[tri_of] + local % bw[tri_of]
        ys = y0[tri_of] + local // bw[tri_of]
        sx, sy = xs + 0.5, ys + 0.5
        ax, ay = a[tri_of, 0], a[tri_of, 1]
        bx, by = b[tri_of, 0], b[tri_of, 1]
        cx, cy = c[tri_of, 0], c[tri_of, 1]
        ar = area[tri_of]
        w0 = ((bx - sx) * (cy - sy) - (by - sy) * (cx - sx)) / ar
        w1 = ((cx - sx) * (ay - sy) - (cy - sy) * (ax - sx)) / ar
        w2 = 1.0 - w0 - w1
        eps = -1e-4
        inside = (w0 >= eps) & (w1 >= eps) & (w2 >= eps)
        if not inside.any():
            continue
        tri_of, w0, w1, w2 = tri_of[inside], w0[inside], w1[inside], w2[inside]
        lin = ys[inside] * w + xs[inside]
        z = w0 * depth[tri_of, 0] + w1 * depth[tri_of, 1] + w2 * depth[tri_of, 2]
        order = np.lexsort((-z, lin))                      # per pixel, nearest first
        lin, z, tri_of = lin[order], z[order], tri_of[order]
        first = np.ones(len(lin), bool)
        first[1:] = lin[1:] != lin[:-1]
        lin, z, tri_of = lin[first], z[first], tri_of[first]
        nearer = z > zbuf[lin]
        zbuf[lin[nearer]] = z[nearer]
        out[lin[nearer]] = colours[tri_of[nearer]]


def best_view_for(point: dict, mesh: trimesh.Trimesh) -> str:
    """Pick the view where a location is most likely visible (problems underneath need the view from below)."""
    lo, hi = mesh.bounds
    rel_z = (point["z"] - lo[2]) / max(hi[2] - lo[2], 1e-6)
    return "below" if rel_z < 0.15 else "iso"
