"""Where each part sits on the plate and how tall it is, read from the G-code.

OrcaSlicer (and older Bambu Studio) label every object's moves with
``; start printing object, unique label id: N`` / ``; stop printing object, ...``.
Bambu Studio 2 writes ``; OBJECT_ID: N`` where an object's moves start instead, with no stop
label: an object's moves then end at the next ``OBJECT_ID``, the prime tower, or the layer change.
ProofLoop tracks extrusion inside each label to get the part's footprint and
the highest Z it was printed at, then cross-checks that height against the
slicer's own ``object max height`` header. Moves outside labels (purge line,
wipe tower, prime) are not parts and are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_START = re.compile(r"^; start printing object, unique label id: (\S+)")
_STOP = re.compile(r"^; stop printing object, unique label id: (\S+)")
_OBJECT_ID = re.compile(r"^; OBJECT_ID: (\d+)")
# Where a Bambu Studio 2 object's moves end (it has no stop label).
_OBJECT_ENDS = (
    "; CHANGE_LAYER",
    "; FEATURE: Prime tower",
    "; FEATURE: Wipe tower",
    "; MACHINE_END_GCODE_START",
)


@dataclass
class Part:
    label: str
    name: str
    xmin: float
    xmax: float
    ymin: float
    ymax: float
    height: float
    slicer_height: float | None = None

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "name": self.name,
            "xmin": round(self.xmin, 2),
            "xmax": round(self.xmax, 2),
            "ymin": round(self.ymin, 2),
            "ymax": round(self.ymax, 2),
            "height": round(self.height, 2),
            "slicer_height": self.slicer_height,
        }


class GeometryError(ValueError):
    pass


def read_parts(
    gcode: str, names: dict[str, str] | None = None, slicer_heights: dict[str, float] | None = None
) -> list[Part]:
    names = names or {}
    slicer_heights = slicer_heights or {}
    boxes: dict[str, list[float]] = {}
    current: str | None = None
    skipping = False
    absolute_xyz = True
    absolute_e = True
    x = y = z = e = 0.0
    for raw in gcode.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line[0] == ";":
            if m := _START.match(line) or _OBJECT_ID.match(line):
                current = m.group(1)
            elif _STOP.match(line) or line.startswith(_OBJECT_ENDS):
                current = None
            elif line == "; SKIPPABLE_START":  # optional moves (a timelapse shot), not the part
                skipping = True
            elif line == "; SKIPPABLE_END":
                skipping = False
            if line == "; MACHINE_END_GCODE_START":
                break
            continue
        code = line.split(";", 1)[0]
        parts = code.split()
        if not parts:
            continue
        head = parts[0].upper()
        if head == "G90":
            absolute_xyz = absolute_e = True
        elif head == "G91":
            absolute_xyz = absolute_e = False
        elif head == "M82":
            absolute_e = True
        elif head == "M83":
            absolute_e = False
        elif head == "G92":
            w = {k.upper(): float(v) for k, v in _WORD.findall(code)}
            x, y, z, e = w.get("X", x), w.get("Y", y), w.get("Z", z), w.get("E", e)
        elif head in ("G0", "G1", "G2", "G3"):
            w = {k.upper(): float(v) for k, v in _WORD.findall(code)}
            nx = w["X"] if "X" in w and absolute_xyz else x + w.get("X", 0.0)
            ny = w["Y"] if "Y" in w and absolute_xyz else y + w.get("Y", 0.0)
            nz = w["Z"] if "Z" in w and absolute_xyz else z + w.get("Z", 0.0)
            de = 0.0
            if "E" in w:
                de = w["E"] - e if absolute_e else w["E"]
                e = w["E"] if absolute_e else e + w["E"]
            if current is not None and de > 0 and not skipping:
                b = boxes.setdefault(current, [nx, nx, ny, ny, nz])
                # Arcs are bounded by their chord here; the outer walls still mark the footprint.
                b[0] = min(b[0], x, nx)
                b[1] = max(b[1], x, nx)
                b[2] = min(b[2], y, ny)
                b[3] = max(b[3], y, ny)
                b[4] = max(b[4], nz)
            x, y, z = nx, ny, nz
    parts_out = [
        Part(
            label,
            names.get(label, f"object {label}"),
            b[0],
            b[1],
            b[2],
            b[3],
            b[4],
            slicer_heights.get(label),
        )
        for label, b in boxes.items()
    ]
    parts_out.sort(key=lambda p: (p.ymin, p.xmin))
    return parts_out


def check_heights(parts: list[Part], tolerance: float = 0.5) -> list[str]:
    """Problems where the G-code disagrees with the slicer's header. Empty list means they agree."""
    problems = []
    for p in parts:
        if p.slicer_height is not None and abs(p.slicer_height - p.height) > tolerance:
            problems.append(
                f"{p.name}: G-code reaches {p.height:.2f} mm but the slicer header says {p.slicer_height:.2f} mm"
            )
    return problems
