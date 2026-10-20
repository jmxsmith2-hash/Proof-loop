"""Synthetic sliced files for tests."""

from __future__ import annotations

import io
import json
import zipfile


def box_gcode(boxes, layer=0.2, start_marker=True, purge=True):
    """Concentric-perimeter G-code for axis-aligned boxes: (x0, y0, x1, y1, height)."""
    out = ["; HEADER_BLOCK_START", "; total estimated time: 1h 2m 3s", "; HEADER_BLOCK_END"]
    out += ["M140 S60", "M190 S60", "G90", "M83", "G28"]
    if purge:
        # Purge line along the front edge, inside the start G-code.
        out += ["G1 Z0.3 F600", "G1 X10 Y1 F3000", "G1 X120 Y1 E8 F1500"]
    if start_marker:
        out.append("; MACHINE_START_GCODE_END")
    z = 0.0
    top = max(b[4] for b in boxes)
    while z < top - 1e-9:
        z = round(z + layer, 3)
        out.append(f"G1 Z{z} F600")
        out.append(f"M73 L{int(round(z / layer))}")
        for x0, y0, x1, y1, h in boxes:
            if z > h + 1e-9:
                continue
            out += [
                f"G1 X{x0} Y{y0} F12000",
                f"G1 X{x1} Y{y0} E1",
                f"G1 X{x1} Y{y1} E1",
                f"G1 X{x0} Y{y1} E1",
                f"G1 X{x0} Y{y0} E1",
                f"G1 X{x0 + 1} Y{y0 + 1} F12000",
                f"G1 X{x1 - 1} Y{y1 - 1} E1",
            ]
    out += ["; MACHINE_END_GCODE_START", "M104 S0", "M140 S0", "G1 Z200 F600", "M73 P100 R0"]
    return "\n".join(out) + "\n"


def make_3mf(
    gcode: str, filaments=("PLA",), printer_model="Bambu Lab P1S", chamber=None, grams=12.5
) -> bytes:
    slice_info = (
        '<?xml version="1.0" encoding="UTF-8"?><config><plate>'
        '<metadata key="index" value="1"/><metadata key="prediction" value="3723"/>'
        + "".join(
            f'<filament id="{i + 1}" type="{t}" color="#FFFFFF" used_m="1" used_g="{grams / len(filaments)}"/>'
            for i, t in enumerate(filaments)
        )
        + "</plate></config>"
    )
    settings = {"printer_model": printer_model, "filament_type": list(filaments)}
    if chamber is not None:
        settings["chamber_temperatures"] = [str(chamber)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", "<model/>")
        z.writestr("Metadata/plate_1.gcode", gcode)
        z.writestr("Metadata/plate_1.gcode.md5", "STALE")
        z.writestr("Metadata/slice_info.config", slice_info)
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        z.writestr("Metadata/plate_1.png", b"\x89PNG")
    return buf.getvalue()
