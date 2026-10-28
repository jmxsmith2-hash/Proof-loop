"""Write example G-code for `synthux physical gcode` (Bambu Studio style, Bambu Lab P2S).

bracket_good.gcode    clean PLA print: should pass with no major findings
bracket_flawed.gcode  the same part with planted flaws:
                      - placed partly off the 256 mm bed (X up to 262 mm)
                      - first layer 0.36 mm with a 0.4 mm nozzle; top layers 0.34 mm
                      - part-cooling fan never switched on (PLA)
                      - infill at 420 mm/s: about 38 mm³/s, more than the hot end melts
                      - 6 mm retractions on a direct-drive extruder
                      - heaters left on at the end
bracket_good.gcode.3mf  the clean print packaged like Bambu Studio's "Export plate sliced file"

Run: python examples/gcode/make_examples.py
"""

from __future__ import annotations

import math
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIL_AREA = math.pi * (1.75 / 2) ** 2


def _e(length: float, width: float, height: float) -> float:
    return length * width * height / FIL_AREA


def slice_box(*, x0: float, y0: float, w: float, d: float, h: float, first: float, layer: float,
              top_layer: float, fan: bool, infill_speed: float, retract: float, heaters_off: bool,
              nozzle_c: int = 220, bed_c: int = 55) -> str:
    out: list[str] = []
    emit = out.append
    layers = [first]
    while layers[-1] < h - 1e-6:
        step = top_layer if layers[-1] > h - 2.0 else layer
        layers.append(round(min(h, layers[-1] + step), 3))
    total_e = 0.0
    body: list[str] = []
    b = body.append
    pos = [0.0, 0.0]

    def travel(x: float, y: float, z: float) -> None:
        dist = math.hypot(x - pos[0], y - pos[1])
        if dist > 2:
            b(f"G1 E-{retract:.2f} F2100")
        b(f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} F30000")
        if dist > 2:
            b(f"G1 E{retract:.2f} F2100")
        pos[:] = [x, y]

    def line(x: float, y: float, width: float, hgt: float, speed: float) -> None:
        nonlocal total_e
        e = _e(math.hypot(x - pos[0], y - pos[1]), width, hgt)
        total_e += e
        b(f"G1 X{x:.3f} Y{y:.3f} E{e:.5f} F{speed * 60:.0f}")
        pos[:] = [x, y]

    prev = 0.0
    for i, z in enumerate(layers):
        hgt = round(z - prev, 3)
        prev = z
        b("; CHANGE_LAYER")
        b(f"; Z_HEIGHT: {z:g}")
        b(f"; LAYER_HEIGHT: {hgt:g}")
        if i == 1 and fan:
            b("M106 P1 S255")
        speed_scale = 0.25 if i == 0 else 1.0
        for k, (name, inset, speed) in enumerate((("Outer wall", 0.21, 150), ("Inner wall", 0.63, 200))):
            b(f"; FEATURE: {name}")
            corners = [(x0 + inset, y0 + inset), (x0 + w - inset, y0 + inset), (x0 + w - inset, y0 + d - inset),
                       (x0 + inset, y0 + d - inset), (x0 + inset, y0 + inset)]
            travel(*corners[0], z)
            for cx, cy in corners[1:]:
                line(cx, cy, 0.42, hgt, speed * speed_scale)
        b("; FEATURE: Sparse infill" if 2 < i < len(layers) - 3 else "; FEATURE: Internal solid infill")
        spacing = 4.0 if 2 < i < len(layers) - 3 else 0.45
        ys = [y0 + 1.0 + k * spacing for k in range(int((d - 2.0) / spacing) + 1)]
        travel(x0 + 1.0, ys[0], z)
        for k, yy in enumerate(ys):
            xa, xb = (x0 + 1.0, x0 + w - 1.0) if k % 2 == 0 else (x0 + w - 1.0, x0 + 1.0)
            if k:
                b(f"G1 X{xa:.3f} Y{yy:.3f} F12000")
                pos[:] = [xa, yy]
            line(xb, yy, 0.45, hgt, (infill_speed if i else 50))
    head = [
        "; HEADER_BLOCK_START",
        "; BambuStudio 02.02.00.00 (synthux example)",
        "; model printing time: 7m 20s; total estimated time: 10m 5s",
        f"; total layer number: {len(layers)}",
        f"; total filament length [mm] : {total_e:.2f}",
        f"; total filament weight [g] : {total_e * FIL_AREA / 1000 * 1.24:.2f}",
        "; filament_diameter: 1.75",
        f"; max_z_height: {layers[-1]:.2f}",
        "; HEADER_BLOCK_END",
        "; CONFIG_BLOCK_START",
        "; printer_model = Bambu Lab P2S",
        "; nozzle_diameter = 0.4",
        "; filament_type = PLA",
        "; filament_settings_id = \"Bambu PLA Basic @BBL P2S\"",
        f"; nozzle_temperature = {nozzle_c}",
        f"; hot_plate_temp = {bed_c}",
        f"; layer_height = {layer}",
        f"; retraction_length = {retract}",
        "; CONFIG_BLOCK_END",
        "; MACHINE_START_GCODE_START",
        "G90", "M83", "M204 S10000",
        f"M140 S{bed_c}", f"M104 S{nozzle_c}", "G28", f"M190 S{bed_c}", f"M109 S{nozzle_c}",
        "; purge line in front of the plate (start code, not checked against the bed)",
        "G1 X18 Y-0.5 Z0.3 F18000", "G1 X120 Y-0.5 E12 F1500", "G1 X200 Y-0.5 E6 F3000", "G1 Z1 F600",
        "G92 E0",
        "; MACHINE_START_GCODE_END",
    ]
    tail = ["; MACHINE_END_GCODE_START", "G1 E-0.8 F1800", f"G1 Z{layers[-1] + 10:.2f} F900", "G1 X128 Y250 F12000",
            "M106 P1 S0"]
    if heaters_off:
        tail += ["M140 S0 ; turn off bed", "M104 S0 ; turn off hotend"]
    tail += ["; MACHINE_END_GCODE_END"]
    return "\n".join(head + body + tail) + "\n"


def main() -> None:
    good = slice_box(x0=100, y0=100, w=60, d=30, h=12, first=0.2, layer=0.2, top_layer=0.2, fan=True,
                     infill_speed=230, retract=0.8, heaters_off=True)
    flawed = slice_box(x0=202, y0=100, w=60, d=30, h=12, first=0.36, layer=0.2, top_layer=0.34, fan=False,
                       infill_speed=420, retract=6.0, heaters_off=False)
    (HERE / "bracket_good.gcode").write_text(good, encoding="utf-8")
    (HERE / "bracket_flawed.gcode").write_text(flawed, encoding="utf-8")
    used_mm = float(good.split("total filament length [mm] : ")[1].split()[0])
    grams = used_mm * FIL_AREA / 1000 * 1.24
    info = ('<?xml version="1.0" encoding="UTF-8"?><config><header><header_item key="X-BBL-Client-Type" value="slicer"/>'
            '</header><plate><metadata key="index" value="1"/><metadata key="printer_model_id" value="N7"/>'
            '<metadata key="nozzle_diameters" value="0.4"/><metadata key="prediction" value="2260"/>'
            f'<metadata key="weight" value="{grams:.2f}"/><filament id="1" type="PLA" color="#F2F2F2" '
            f'used_m="{used_mm / 1000:.2f}" used_g="{grams:.2f}"/></plate></config>')
    with zipfile.ZipFile(HERE / "bracket_good.gcode.3mf", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/plate_1.gcode", good)
        z.writestr("Metadata/slice_info.config", info)
    print("wrote", ", ".join(p.name for p in sorted(HERE.glob("bracket_*"))))


if __name__ == "__main__":
    main()
