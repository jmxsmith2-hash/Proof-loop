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
    rot = _view_matrix(*VIEWS[view])
    centre = mesh.bounds.mean(axis=0)
    verts = (mesh.vertices - centre) @ rot.T
    faces = mesh.faces
    if len(faces) > 150_000:  # keep drawing time bounded on huge meshes
        keep = np.argsort(-mesh.area_faces)[:150_000]
        faces = faces[keep]
        highlight = highlight[keep] if highlight is not None else None
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

    img = Image.new("RGB", (w, h), (248, 248, 246))
    draw = ImageDraw.Draw(img)
    order = np.argsort(tri[:, :, 2].mean(axis=1))  # far to near
    px = to_px(tri)
    base = np.array([150, 170, 195], dtype=float)
    hot = np.array([230, 90, 70], dtype=float)
    for i in order:
        if not facing[i]:
            continue
        colour = hot if highlight is not None and highlight[i] else base
        c = tuple(int(v) for v in np.clip(colour * shade[i], 0, 255))
        draw.polygon([tuple(px[i, k]) for k in range(3)], fill=c, outline=c)
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


def best_view_for(point: dict, mesh: trimesh.Trimesh) -> str:
    """Pick the view where a location is most likely visible (problems underneath need the view from below)."""
    lo, hi = mesh.bounds
    rel_z = (point["z"] - lo[2]) / max(hi[2] - lo[2], 1e-6)
    return "below" if rel_z < 0.15 else "iso"
