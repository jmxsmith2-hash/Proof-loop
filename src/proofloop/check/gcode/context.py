"""What the G-code checks share: the context they run on and the per-move columns derived from the trace."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from proofloop.check.cad.context import CadFinding
from proofloop.check.gcode.parse import _spread

DEFAULT_ACCEL = 5000.0
COLD_EXTRUSION_C = 170.0         # Marlin/Bambu refuse to extrude below about this


def _unset() -> np.ndarray:
    return np.zeros(0)       # filled in by prepare()


@dataclass
class GcodeContext:
    trace: Any
    printer: dict[str, Any]
    material: dict[str, Any]
    nozzle: float
    filament_diameter: float
    findings: list[CadFinding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    # derived columns
    xy: np.ndarray = field(default_factory=_unset, repr=False)
    model: np.ndarray = field(default_factory=_unset, repr=False)
    extrude: np.ndarray = field(default_factory=_unset, repr=False)
    travel: np.ndarray = field(default_factory=_unset, repr=False)
    time: np.ndarray = field(default_factory=_unset, repr=False)
    layer_z: np.ndarray = field(default_factory=_unset, repr=False)
    layer_h: np.ndarray = field(default_factory=_unset, repr=False)
    layer_loop: np.ndarray = field(default_factory=_unset, repr=False)
    move_h: np.ndarray = field(default_factory=_unset, repr=False)
    flow: np.ndarray = field(default_factory=_unset, repr=False)
    width: np.ndarray = field(default_factory=_unset, repr=False)
    images: dict[str, str] = field(default_factory=dict)
    cache: dict[Any, Any] = field(default_factory=dict, repr=False)
    history: dict[str, Any] | None = None     # what real prints on this printer + material taught

    @property
    def label(self) -> str:
        return (self.printer.get("names") or [self.printer["name"]])[0]

    @property
    def fil_area(self) -> float:
        return math.pi * (self.filament_diameter / 2) ** 2

    def add(self, check: str, severity: int, title: str, detail: str, fix: str = "", *, layer: int | None = None,
            at: tuple[float, float, float] | None = None, image: str | None = None, **data: Any) -> None:
        loc = {"x": round(at[0], 1), "y": round(at[1], 1), "z": round(at[2], 2)} if at else None
        if layer is not None:
            data["layer"] = int(layer) + 1                       # across the whole file (pictures use it)
            if self.metrics.get("loops", 1) > 1 and 0 <= layer < len(self.layer_loop):
                lp = self.layer_loop[layer]
                order = sorted(set(self.layer_loop[~np.isnan(self.layer_h)].tolist()))
                firsts = np.flatnonzero((self.layer_loop == lp) & ~np.isnan(self.layer_h))
                data["loop_no"] = order.index(int(lp)) + 1 if int(lp) in order else None
                data["layer_in_loop"] = int(layer - firsts[0]) + 1 if len(firsts) else None
        if image:
            data["image_key"] = image
        self.findings.append(CadFinding(check, severity, title, detail, fix, loc, data, fingerprint=check))


def prepare(ctx: GcodeContext) -> None:
    t = ctx.trace
    ctx.xy = np.hypot(t.x1 - t.x0, t.y1 - t.y0)
    ctx.extrude = (t.de > 1e-7) & (ctx.xy > 1e-6)
    ctx.travel = (t.de <= 1e-7) & (ctx.xy > 1e-6)
    # Bambu files mark where the print starts and ends: start code (purge) and end code (park, a loop
    # file's ejection sweep) are left out of the print checks.
    printing = (t.phase == 1) if (t.phase is not None and t.phase_marked) else None
    if t.layer_markers:
        ctx.model = t.layer >= 0
        if printing is not None:
            ctx.model &= printing
        layer = t.layer.copy()
    else:   # no markers: every rise in extrusion height is a new layer; each loop starts on the bed again
        ctx.model = printing if printing is not None else np.ones(len(t), bool)
        zs = np.round(t.z, 3)
        layer = np.zeros(len(t), np.int32)
        base = 0
        loops = t.loop if t.loop is not None else np.zeros(len(t), np.int32)
        for lp in np.unique(loops):
            idx = np.flatnonzero(loops == lp)
            ext = ctx.extrude[idx] & ctx.model[idx]
            cur = np.fmax.accumulate(np.where(ext, zs[idx], -1.0))
            uniq = np.unique(cur[ext]) if ext.any() else np.zeros(0)
            layer[idx] = np.searchsorted(uniq, cur) + base
            base += len(uniq)
        t.layer = layer
    n_layers = int(layer.max()) + 1 if len(layer) and layer.max() >= 0 else 0
    sel = ctx.extrude & ctx.model
    lz = np.full(n_layers, np.nan)
    layer_loop = np.zeros(n_layers, np.int32)
    if sel.any():
        idx = layer[sel]
        if t.loop is not None:
            layer_loop[idx] = t.loop[sel]
        order = np.argsort(idx, kind="stable")
        zs, idx = t.z[sel][order], idx[order]
        starts = np.searchsorted(idx, np.arange(n_layers))
        ends = np.searchsorted(idx, np.arange(n_layers), side="right")
        for i in range(n_layers):
            if ends[i] > starts[i]:
                lz[i] = float(np.median(zs[starts[i]:ends[i]]))
    keep = ~np.isnan(lz)
    ctx.layer_z = lz
    ctx.layer_loop = layer_loop
    heights = np.full(n_layers, np.nan)
    prev, prev_loop = 0.0, None
    for j in np.flatnonzero(keep):
        if layer_loop[j] != prev_loop:          # a new loop prints its first layer on the bed again
            prev, prev_loop = 0.0, layer_loop[j]
        heights[j] = lz[j] - prev
        prev = lz[j]
    ctx.layer_h = heights
    ctx.metrics["loops"] = max(1, len(np.unique(layer_loop[keep]))) if keep.any() else 1
    li = np.clip(layer, 0, max(0, n_layers - 1))
    ctx.move_h = heights[li] if n_layers else np.full(len(t), 0.2)
    ctx.move_h = np.where(np.isnan(ctx.move_h) | (ctx.move_h <= 0), ctx.nozzle / 2, ctx.move_h)

    v = np.maximum(t.feed, 0.1)
    a = np.where(t.accel > 0, t.accel, float(ctx.printer.get("accel_mm_s2", DEFAULT_ACCEL)))
    naive = t.length / v
    full = t.length >= v * v / a
    stop = np.where(full, t.length / v + v / a, 2 * np.sqrt(t.length / a))
    ctx.time = (naive + stop) / 2          # between "corners at full speed" and "stop at every corner"
    vol = np.where(ctx.extrude, t.de * ctx.fil_area, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        ctx.flow = np.where(ctx.extrude, vol * v / ctx.xy, 0.0)
        ctx.width = np.where(ctx.extrude, vol / (ctx.xy * ctx.move_h), 0.0)


def _at_moves(events: list[tuple], n: int, default: float = np.nan) -> np.ndarray:
    """The value set by the last event before each move (events apply to the moves after them)."""
    return _spread(events, n, default)


def _where(ctx: GcodeContext, i: int) -> tuple[float, float, float]:
    t = ctx.trace
    return float(t.x1[i]), float(t.y1[i]), float(t.z[i])
