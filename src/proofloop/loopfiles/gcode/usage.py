"""Filament used up to each layer, from the plate's own G-code.

Bambu slices use relative extrusion (``M83``), so the filament a print has
used is the sum of every ``E`` value on ``G0``-``G3`` moves, retractions
included (they come back out with the next un-retract). On the real P2S
evidence plate that sum, converted with the file's own diameter and density,
gives 53.887 g against the slicer's 53.89 g. Layers are counted at Bambu's
``M73 L<n>`` progress marker, the same number the printer reports as
``layer_num``. Tool changes (``T<n>``) move the count to filament slot n + 1.

This is only used for a loop that stops part-way (failed or cancelled): a
loop that finishes is charged the slicer's own grams.
"""

from __future__ import annotations

import math
import re

_E = re.compile(r"\sE(-?\d*\.?\d+)")
_T = re.compile(r"^T(\d+)\s*(?:;|$)")


def layer_usage(
    gcode: str, slots: list[str], diameter: dict[str, float], density: dict[str, float]
) -> dict | None:
    """``{"layers": n, "grams": {slot: [grams used by the end of layer 1, 2, ... n]}}``.

    None when the file lacks what the sum needs (relative extrusion, a diameter
    and density for every slot); usage is then never estimated.
    """
    if not slots or "\nM83" not in gcode:
        return None
    if any(not diameter.get(s) or not density.get(s) for s in slots):
        return None
    start = gcode.find("; EXECUTABLE_BLOCK_START")
    end = gcode.find("; EXECUTABLE_BLOCK_END")
    body = gcode[start if start >= 0 else 0 : end if end >= 0 else len(gcode)]
    mm = {s: 0.0 for s in slots}
    per_layer: list[dict[str, float]] = []
    current = slots[0]
    by_marker = "\nM73 L" not in body  # no layer numbers from the slicer: count its "; CHANGE_LAYER" comments
    started = False
    for line in body.splitlines():
        if not line:
            continue
        if line[0] == ";":
            if by_marker and line.startswith("; CHANGE_LAYER"):
                if started:
                    per_layer.append(dict(mm))  # the end of the previous layer
                started = True
            continue
        if line.startswith("M73 L"):
            if mm and line[5:].split()[0].isdigit():
                n = int(line[5:].split()[0])
                if n > 1 and n - 1 > len(per_layer):
                    per_layer.append(dict(mm))  # snapshot at the end of the previous layer
            continue
        if line[0] == "T":
            m = _T.match(line)
            if m:
                k = str(int(m.group(1)) + 1)
                if k in mm:
                    current = k
            continue
        if line[0] == "G" and line[1:3] in ("0 ", "1 ", "2 ", "3 "):
            m = _E.search(line.split(";", 1)[0])
            if m:
                mm[current] += float(m.group(1))
    per_layer.append(dict(mm))
    area = {s: math.pi * (diameter[s] / 2) ** 2 for s in slots}
    return {
        "layers": len(per_layer),
        "grams": {
            s: [round(max(0.0, row[s]) * area[s] * density[s] / 1000, 3) for row in per_layer] for s in slots
        },
    }


def used_by_layer(usage: dict | None, layer: int) -> dict[str, float] | None:
    """Grams per slot once ``layer`` layers are complete (0 = none)."""
    if not usage:
        return None
    if layer <= 0:
        return {s: 0.0 for s in usage["grams"]}
    i = min(layer, usage["layers"]) - 1
    return {s: g[i] for s, g in usage["grams"].items()}
