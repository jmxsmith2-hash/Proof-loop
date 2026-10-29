"""Pictures of a G-code print: the plate, single layers, a side view, a flow map and the travels."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from proofloop.check.gcode.checks import GcodeContext

SIZE = 520
BG = (250, 250, 247)
BED = (228, 226, 219)
FEATURE_COLOURS = {"outer wall": (214, 96, 26), "inner wall": (236, 170, 40), "external perimeter": (214, 96, 26),
                   "perimeter": (236, 170, 40), "wall-outer": (214, 96, 26), "wall-inner": (236, 170, 40),
                   "sparse infill": (150, 60, 60), "internal infill": (150, 60, 60), "fill": (150, 60, 60),
                   "internal solid infill": (180, 80, 140), "solid infill": (180, 80, 140), "skin": (180, 80, 140),
                   "top surface": (200, 40, 80), "bottom surface": (110, 90, 170), "bridge": (40, 120, 200),
                   "support": (60, 150, 80), "support interface": (40, 110, 60), "brim": (110, 110, 110),
                   "skirt": (110, 110, 110), "prime tower": (120, 120, 160)}
DEFAULT = (90, 90, 95)


def _colour(name: str) -> tuple[int, int, int]:
    for key, col in FEATURE_COLOURS.items():
        if key in name:
            return col
    return DEFAULT


class _View:
    def __init__(self, x0: float, y0: float, x1: float, y1: float, size: int = SIZE, pad: int = 14):
        span = max(x1 - x0, y1 - y0, 1e-3)
        self.s = (size - 2 * pad) / span
        self.x0, self.y0, self.pad = x0, y0, pad
        self.w = int(round((x1 - x0) * self.s)) + 2 * pad
        self.h = int(round((y1 - y0) * self.s)) + 2 * pad

    def pt(self, x: float, y: float) -> tuple[float, float]:   # Y up on screen
        return (self.pad + (x - self.x0) * self.s, self.h - self.pad - (y - self.y0) * self.s)


def _draw_layer(ctx: GcodeContext, layer: int, view: _View, draw: ImageDraw.ImageDraw, *, colour_by_flow: bool = False,
                travels: bool = False) -> None:
    t = ctx.trace
    sel = np.flatnonzero(ctx.extrude & (t.layer == layer))
    if colour_by_flow:
        hot = float(ctx.printer.get("max_flow_mm3_s") or 20)
    width = max(1, int(round(ctx.nozzle * view.s * 0.9)))
    for i in sel:
        if colour_by_flow:
            r = min(1.0, ctx.flow[i] / hot)
            col = (int(40 + 200 * r), int(170 - 130 * r), 60) if ctx.flow[i] <= hot * 1.05 else (220, 20, 20)
        else:
            col = _colour(t.features[t.feature[i]])
        draw.line([view.pt(t.x0[i], t.y0[i]), view.pt(t.x1[i], t.y1[i])], fill=col, width=width)
    if travels:
        from proofloop.check.gcode.checks import travels_over_air, unretracted_travels

        for i in travels_over_air(ctx, unretracted_travels(ctx) & (t.layer == layer)):
            draw.line([view.pt(t.x0[i], t.y0[i]), view.pt(t.x1[i], t.y1[i])], fill=(220, 20, 20), width=2)


def plate(ctx: GcodeContext, out: Path, layer: int = 0) -> Path:
    bx, by = (float(v) for v in ctx.printer["build_volume"][:2])
    t = ctx.trace
    sel = ctx.extrude & ctx.model
    xs = np.concatenate([t.x0[sel], t.x1[sel], [0, bx]])
    ys = np.concatenate([t.y0[sel], t.y1[sel], [0, by]])
    view = _View(float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
    img = Image.new("RGB", (view.w, view.h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([view.pt(0, by), view.pt(bx, 0)], fill=BED, outline=(160, 158, 150), width=2)
    _draw_layer(ctx, layer, view, d)
    # whatever is printed past the plate edge turns red
    px = np.asarray(img).copy()
    (left, top), (right, bottom) = view.pt(0, by), view.pt(bx, 0)
    yy, xx = np.mgrid[0:px.shape[0], 0:px.shape[1]]
    off_plate = (xx < left - 1) | (xx > right + 1) | (yy < top - 1) | (yy > bottom + 1)
    inked = np.abs(px.astype(int) - np.array(BG)).sum(axis=2) > 30
    px[off_plate & inked] = (220, 20, 20)
    return _save(Image.fromarray(px), out)


def layer_view(ctx: GcodeContext, layer: int, out: Path, *, colour_by_flow: bool = False, travels: bool = False) -> Path:
    t = ctx.trace
    sel = ctx.extrude & ctx.model
    xs, ys = np.concatenate([t.x0[sel], t.x1[sel]]), np.concatenate([t.y0[sel], t.y1[sel]])
    m = 3.0
    view = _View(float(xs.min()) - m, float(ys.min()) - m, float(xs.max()) + m, float(ys.max()) + m)
    img = Image.new("RGB", (view.w, view.h), BG)
    _draw_layer(ctx, layer, view, ImageDraw.Draw(img), colour_by_flow=colour_by_flow, travels=travels)
    return _save(img, out)


def side_view(ctx: GcodeContext, out: Path) -> Path:
    """X against Z: each layer as a band, too-thick layers in red, fan-off layers in blue outline."""
    t = ctx.trace
    sel = ctx.extrude & ctx.model
    xs = np.concatenate([t.x0[sel], t.x1[sel]])
    top = float(np.nanmax(ctx.layer_z)) if np.isfinite(ctx.layer_z).any() else 1.0
    view = _View(float(xs.min()) - 2, -0.5, float(xs.max()) + 2, top + 1.0)
    img = Image.new("RGB", (view.w, view.h), BG)
    d = ImageDraw.Draw(img)
    d.line([view.pt(view.x0, 0), view.pt(view.x0 + (view.w - 2 * view.pad) / view.s, 0)], fill=(160, 158, 150), width=2)
    layers = t.layer[sel]
    order = np.argsort(layers, kind="stable")
    lx = np.minimum(t.x0[sel], t.x1[sel])[order]
    hx = np.maximum(t.x0[sel], t.x1[sel])[order]
    ls = layers[order]
    starts = np.searchsorted(ls, np.arange(len(ctx.layer_z)))
    ends = np.searchsorted(ls, np.arange(len(ctx.layer_z)), side="right")
    for i, z in enumerate(ctx.layer_z):
        if np.isnan(z) or ends[i] <= starts[i]:
            continue
        h = float(ctx.layer_h[i])
        col = (220, 40, 40) if h > 0.8 * ctx.nozzle + 1e-6 else (120, 120, 125) if i % 2 else (150, 150, 155)
        a, b = view.pt(float(lx[starts[i]:ends[i]].min()), z), view.pt(float(hx[starts[i]:ends[i]].max()), z - h)
        d.rectangle([a[0], a[1], b[0], max(a[1] + 1, b[1])], fill=col)
    return _save(img, out)


def _save(img: Image.Image, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, optimize=True)
    return out


def render_all(ctx: GcodeContext, out_dir: Path) -> dict[str, str]:
    """Write the pictures the report uses; returns {name: relative path}."""
    t = ctx.trace
    valid = np.flatnonzero(~np.isnan(ctx.layer_h))
    if not len(valid):
        return {}
    if ctx.layer_loop is not None:
        valid = valid[ctx.layer_loop[valid] == ctx.layer_loop[valid[0]]]   # a loop file: its first loop
    images: dict[str, str] = {}
    first = int(valid[0])
    images["plate"] = plate(ctx, out_dir / "plate.png", first).name
    images["layer1"] = layer_view(ctx, first, out_dir / "layer_1.png").name
    for frac in (0.5, 1.0):
        pos = min(len(valid) - 1, int(round(frac * (len(valid) - 1))))
        li = int(valid[pos])
        images[f"layer{pos + 1}"] = layer_view(ctx, li, out_dir / f"layer_{pos + 1}.png").name
    images["side"] = side_view(ctx, out_dir / "side.png").name
    for f in ctx.findings:
        key, layer = f.data.get("image_key"), f.data.get("layer")
        if key == "flow" and layer:
            images["flow"] = layer_view(ctx, layer - 1, out_dir / "flow.png", colour_by_flow=True).name
        elif key == "travel" and layer:
            images["travel"] = layer_view(ctx, layer - 1, out_dir / "travel.png", travels=True).name
    del t
    return images
