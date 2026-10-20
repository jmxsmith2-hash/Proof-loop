"""Reconstruct where parts sit on the plate, and how tall they are, from G-code.

Works for Bambu Studio, OrcaSlicer and PrusaSlicer output because it only reads
motion. Each extruding move marks the grid cells it passes over with the current
Z; connected cells become objects. The machine start G-code (purge line, nozzle
wipe) is skipped when the slicer marks it, and moves off the plate are ignored.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

_WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")

START_END_MARKERS = ("; MACHINE_START_GCODE_END", ";MACHINE_START_GCODE_END")
END_START_MARKERS = ("; MACHINE_END_GCODE_START", ";MACHINE_END_GCODE_START")


@dataclass
class ObjectBox:
    xmin: float
    xmax: float
    ymin: float
    ymax: float
    height: float
    footprint_mm2: float

    @property
    def width(self) -> float:
        return self.xmax - self.xmin

    @property
    def depth(self) -> float:
        return self.ymax - self.ymin

    def as_dict(self) -> dict:
        return {
            "xmin": round(self.xmin, 2),
            "xmax": round(self.xmax, 2),
            "ymin": round(self.ymin, 2),
            "ymax": round(self.ymax, 2),
            "height": round(self.height, 2),
            "footprint_mm2": round(self.footprint_mm2, 1),
        }


@dataclass
class HeightMap:
    cell: float
    grid: np.ndarray  # [ny, nx] max extruded Z per cell, 0 where empty

    @property
    def max_height(self) -> float:
        return float(self.grid.max()) if self.grid.size else 0.0

    def objects(self, min_cells: int = 1) -> list[ObjectBox]:
        return _components(self.grid, self.cell, min_cells)

    def ascii(self, width: int = 64) -> str:
        """Top-down preview, back of the plate at the top."""
        ny, nx = self.grid.shape
        step = max(1, math.ceil(nx / width))
        top = self.max_height or 1.0
        shades = " .:-=+*#%@"
        rows = []
        ystep = step * 2  # terminal cells are about twice as tall as they are wide
        for y in range(ny - 1, -1, -ystep):
            row = ""
            for x in range(0, nx, step):
                block = self.grid[max(0, y - ystep + 1) : y + 1, x : x + step]
                h = float(block.max()) if block.size else 0.0
                row += " " if h <= 0 else shades[min(len(shades) - 1, 1 + int((h / top) * (len(shades) - 2)))]
            rows.append(row.rstrip())
        return "\n".join(rows)


def build_heightmap(gcode: str, bed_x: float, bed_y: float, cell: float = 2.0) -> HeightMap:
    nx, ny = max(1, math.ceil(bed_x / cell)), max(1, math.ceil(bed_y / cell))
    grid = np.zeros((ny, nx), dtype=np.float32)

    has_start_marker = any(m in gcode for m in START_END_MARKERS)
    active = not has_start_marker

    absolute_xyz = True
    absolute_e = True
    x = y = z = e = 0.0

    for raw in gcode.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            if not active and line.startswith(START_END_MARKERS):
                active = True
            elif active and line.startswith(END_START_MARKERS):
                break
            continue
        code = line.split(";", 1)[0]
        words = {k.upper(): float(v) for k, v in _WORD.findall(code)}
        head = code.split(None, 1)[0].upper() if code.split() else ""
        if head == "G90":
            absolute_xyz = True
            absolute_e = True
        elif head == "G91":
            absolute_xyz = False
            absolute_e = False
        elif head == "M82":
            absolute_e = True
        elif head == "M83":
            absolute_e = False
        elif head == "G92":
            x = words.get("X", x)
            y = words.get("Y", y)
            z = words.get("Z", z)
            e = words.get("E", e)
        elif head in ("G0", "G1", "G2", "G3"):
            # Arcs are treated as chords; at a 2 mm grid that is close enough.
            nx_, ny_, nz_ = x, y, z
            if "X" in words:
                nx_ = words["X"] if absolute_xyz else x + words["X"]
            if "Y" in words:
                ny_ = words["Y"] if absolute_xyz else y + words["Y"]
            if "Z" in words:
                nz_ = words["Z"] if absolute_xyz else z + words["Z"]
            de = 0.0
            if "E" in words:
                de = words["E"] - e if absolute_e else words["E"]
                e = words["E"] if absolute_e else e + words["E"]
            if active and de > 0 and (nx_ != x or ny_ != y) and nz_ > 0:
                _rasterize(grid, cell, x, y, nx_, ny_, nz_, bed_x, bed_y)
            x, y, z = nx_, ny_, nz_
    return HeightMap(cell=cell, grid=grid)


def _rasterize(grid, cell, x0, y0, x1, y1, zval, bed_x, bed_y) -> None:
    length = math.hypot(x1 - x0, y1 - y0)
    steps = max(1, int(length / (cell / 2)))
    ny, nx = grid.shape
    for i in range(steps + 1):
        t = i / steps
        px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        if not (0 <= px <= bed_x and 0 <= py <= bed_y):
            continue
        cx, cy = min(nx - 1, int(px / cell)), min(ny - 1, int(py / cell))
        if grid[cy, cx] < zval:
            grid[cy, cx] = zval


def _components(grid: np.ndarray, cell: float, min_cells: int) -> list[ObjectBox]:
    ny, nx = grid.shape
    seen = np.zeros_like(grid, dtype=bool)
    out: list[ObjectBox] = []
    occupied = grid > 0
    for sy, sx in zip(*(a.tolist() for a in np.nonzero(occupied)), strict=True):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        cells = []
        while stack:
            cy, cx = stack.pop()
            cells.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    yy, xx = cy + dy, cx + dx
                    if 0 <= yy < ny and 0 <= xx < nx and occupied[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True
                        stack.append((yy, xx))
        if len(cells) < min_cells:
            continue
        ys = [c[0] for c in cells]
        xs = [c[1] for c in cells]
        out.append(
            ObjectBox(
                xmin=float(min(xs) * cell),
                xmax=float((max(xs) + 1) * cell),
                ymin=float(min(ys) * cell),
                ymax=float((max(ys) + 1) * cell),
                height=float(max(grid[c] for c in cells)),
                footprint_mm2=len(cells) * cell * cell,
            )
        )
    out.sort(key=lambda o: (o.ymin, o.xmin))
    return out
