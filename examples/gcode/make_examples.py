"""Write example G-code for `proofloop check gcode` (Bambu Studio style, Bambu Lab P2S).

bracket_good.gcode    clean PLA print: should pass with no major findings
bracket_flawed.gcode  the same part with planted flaws:
                      - placed partly off the 256 mm bed (X up to 262 mm)
                      - first layer 0.36 mm with a 0.4 mm nozzle; top layers 0.34 mm
                      - part-cooling fan never switched on (PLA)
                      - infill at 420 mm/s: about 38 mm³/s, more than the hot end melts
                      - 6 mm retractions on a direct-drive extruder
                      - heaters left on at the end
bracket_good.gcode.3mf  the clean print packaged like Bambu Studio's "Export plate sliced file"
bracket_plate.gcode.3mf a clean two-part plate in Bambu Studio's full layout (header, config and
                      executable blocks, object labels, stock P2S end code), as Make a loop file reads it
FL_S2_bracket_plate.gcode.3mf  that plate made into a 3-loop FarmLoop Stage 2 file by ProofLoop
FL_S2_farmloop_app.gcode.3mf   the same plate as the FarmLoop app 2.72 writes it, with its two
                      documented problems: a 2-hour "natural cooldown" before the print with the
                      heaters on, and one push height (tallest part - 10 mm) that passes over the
                      12 mm part

Run: python examples/gcode/make_examples.py
"""

from __future__ import annotations

import hashlib
import math
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIL_AREA = math.pi * (1.75 / 2) ** 2


def _e(length: float, width: float, height: float) -> float:
    return length * width * height / FIL_AREA


def _layers(h: float, first: float, layer: float, top_layer: float) -> list[float]:
    layers = [first]
    while layers[-1] < h - 1e-6:
        step = top_layer if layers[-1] > h - 2.0 else layer
        layers.append(round(min(h, layers[-1] + step), 3))
    return layers


def _body(boxes: list[tuple], layers: list[float], *, fan: bool, infill_speed: float, retract: float,
          labels: bool) -> tuple[list[str], float]:
    """Walls and infill for each box (label, x0, y0, w, d, h) on each layer it reaches."""
    body: list[str] = []
    b = body.append
    pos = [0.0, 0.0]
    total = [0.0]

    def travel(x: float, y: float, z: float) -> None:
        dist = math.hypot(x - pos[0], y - pos[1])
        if dist > 2:
            b(f"G1 E-{retract:.2f} F2100")
        b(f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} F30000")
        if dist > 2:
            b(f"G1 E{retract:.2f} F2100")
        pos[:] = [x, y]

    def line(x: float, y: float, width: float, hgt: float, speed: float) -> None:
        e = _e(math.hypot(x - pos[0], y - pos[1]), width, hgt)
        total[0] += e
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
        for label, x0, y0, w, d, h in boxes:
            if z > h + 1e-6:
                continue
            top = sum(1 for zz in layers if zz <= h + 1e-6)     # this box's own layer count
            if labels:
                b(f"; start printing object, unique label id: {label}")
            for name, inset, speed in (("Outer wall", 0.21, 150), ("Inner wall", 0.63, 200)):
                b(f"; FEATURE: {name}")
                corners = [(x0 + inset, y0 + inset), (x0 + w - inset, y0 + inset), (x0 + w - inset, y0 + d - inset),
                           (x0 + inset, y0 + d - inset), (x0 + inset, y0 + inset)]
                travel(*corners[0], z)
                for cx, cy in corners[1:]:
                    line(cx, cy, 0.42, hgt, speed * speed_scale)
            sparse = 2 < i < top - 3
            b("; FEATURE: Sparse infill" if sparse else "; FEATURE: Internal solid infill")
            spacing = 4.0 if sparse else 0.45
            ys = [y0 + 1.0 + k * spacing for k in range(int((d - 2.0) / spacing) + 1)]
            travel(x0 + 1.0, ys[0], z)
            for k, yy in enumerate(ys):
                xa, xb = (x0 + 1.0, x0 + w - 1.0) if k % 2 == 0 else (x0 + w - 1.0, x0 + 1.0)
                if k:
                    b(f"G1 X{xa:.3f} Y{yy:.3f} F12000")
                    pos[:] = [xa, yy]
                line(xb, yy, 0.45, hgt, (infill_speed if i else 50))
            if labels:
                b(f"; stop printing object, unique label id: {label}")
    return body, total[0]


def slice_box(*, x0: float, y0: float, w: float, d: float, h: float, first: float, layer: float,
              top_layer: float, fan: bool, infill_speed: float, retract: float, heaters_off: bool,
              nozzle_c: int = 220, bed_c: int = 55) -> str:
    layers = _layers(h, first, layer, top_layer)
    body, total_e = _body([("1", x0, y0, w, d, h)], layers, fan=fan, infill_speed=infill_speed, retract=retract,
                          labels=False)
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


P2S_END = """;======== P2S end gcode ==========
M400 ; wait for buffer to clear
G92 E0 ; zero the extruder
M211 Z1
G90
G1 Z{z} F900 ; lower z a little
M140 S0 ; turn off bed
M106 S0 ; turn off fan
M106 P2 S0 ; turn off remote part cooling fan
M106 P3 S0 ; turn off chamber cooling fan
M104 S0 ; turn off hotend
M400 ; wait all motion done
M17 S
M17 Z0.4 ; lower z motor current to reduce impact if there is something in the bottom
G1 Z{z2} F600
M400 P100
M17 R ; restore z current
M400
M18"""

PLATE_BOXES = [("101", 50.0, 100.0, 60.0, 30.0, 12.0), ("102", 140.0, 90.0, 50.0, 40.0, 24.0)]


def bambu_plate(boxes: list[tuple] = PLATE_BOXES, nozzle_c: int = 220, bed_c: int = 55) -> tuple[str, float]:
    """A plate in Bambu Studio's full layout, which ClearLoop and the printer's firmware expect."""
    top = max(bx[5] for bx in boxes)
    layers = _layers(top, 0.2, 0.2, 0.2)
    body, total_e = _body(boxes, layers, fan=True, infill_speed=230, retract=0.8, labels=True)
    grams = total_e * FIL_AREA / 1000 * 1.24
    head = [
        "; HEADER_BLOCK_START",
        "; BambuStudio 02.02.00.00 (synthux example)",
        "; model printing time: 24m 10s; total estimated time: 27m 30s",
        f"; total layer number: {len(layers)}",
        f"; total filament length [mm] : {total_e:.2f}",
        f"; total filament weight [g] : {grams:.2f}",
        "; filament_diameter: 1.75",
        f"; model label id: {','.join(bx[0] for bx in boxes)}",
        f"; object max height: {','.join(f'{bx[5]:.2f}' for bx in boxes)}",
        f"; max_z_height: {top:.2f}",
        "; HEADER_BLOCK_END",
        "",
        "; CONFIG_BLOCK_START",
        "; printer_model = Bambu Lab P2S",
        "; curr_bed_type = Textured PEI Plate",
        "; printable_area = 0x0,256x0,256x256,0x256",
        "; printable_height = 256",
        "; nozzle_diameter = 0.4",
        "; filament_type = PLA",
        '; filament_settings_id = "Bambu PLA Basic @BBL P2S"',
        '; filament_vendor = "Bambu Lab"',
        "; filament_density = 1.24",
        "; filament_diameter = 1.75",
        f"; nozzle_temperature = {nozzle_c}",
        f"; hot_plate_temp = {bed_c}",
        "; layer_height = 0.2",
        "; retraction_length = 0.8",
        "; machine_max_speed_x = 1000,1000",
        "; machine_max_speed_z = 20,20",
        "; CONFIG_BLOCK_END",
        "",
        "; EXECUTABLE_BLOCK_START",
        "M73 P0 R27",
        "M106 S0",
        ";M1002 set_flag extrude_cali_flag=1",           # the P2S start template's own header lines
        "",
        ";======== P2S start gcode==========",
        ";===== 2026/05/18 =====",
        "",
        f"  M140 S{bed_c} ; heat heatbed first",
        "  M400",
        "G90", "M83", "M204 S10000",
        f"M104 S{nozzle_c}", "G28", f"M190 S{bed_c}", f"M109 S{nozzle_c}",
        "; purge line in front of the plate, not an object",
        "G1 X18 Y1 Z0.3 F18000", "G1 X120 Y1 E12 F1500", "G1 Z1 F600",
        "G92 E0",
        "; MACHINE_START_GCODE_END",
    ]
    tail = ["; MACHINE_END_GCODE_START", P2S_END.format(z=top + 10, z2=top + 4), "M73 P100 R0",
            "; EXECUTABLE_BLOCK_END", ""]
    return "\n".join(head + body + tail) + "\n", grams


def plate_3mf(gcode: str, grams: float, boxes: list[tuple] = PLATE_BOXES) -> bytes:
    import io

    data = gcode.encode()
    objects = "".join(f'<object identify_id="{bx[0]}" name="Part {bx[0]}" skipped="false" />' for bx in boxes)
    info = ('<?xml version="1.0" encoding="UTF-8"?><config><plate><metadata key="index" value="1"/>'
            '<metadata key="printer_model_id" value="N7"/><metadata key="nozzle_diameters" value="0.4"/>'
            f'<metadata key="prediction" value="1650"/><metadata key="weight" value="{grams:.2f}"/>{objects}'
            f'<filament id="1" type="PLA" color="#F2F2F2" used_m="1" used_g="{grams:.2f}"/></plate></config>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", "<model/>")
        z.writestr("Metadata/plate_1.gcode", data)
        z.writestr("Metadata/plate_1.gcode.md5", hashlib.md5(data).hexdigest().upper())
        z.writestr("Metadata/slice_info.config", info)
    return buf.getvalue()


def loop_file(plate: bytes, loops: int = 3) -> tuple[str, bytes]:
    """The plate as a FarmLoop Stage 2 loop file made by ProofLoop."""
    from proofloop.loopfiles import threemf
    from proofloop.loopfiles.gcode.processor import process
    from proofloop.loopfiles.gcode.routines import LoopSettings
    from proofloop.loopfiles.profiles import ProfileStore

    sliced = threemf.loads(plate, name="bracket_plate.gcode.3mf")
    result = process(sliced, ProfileStore().get("p2s-stage2-farmboard"),
                     LoopSettings(loops=loops, wait_for_monitor=True))
    return result.filename, result.data


def farmloop_app_file(plate: str, loops: int = 2) -> str:
    """The plate as the FarmLoop app 2.72 lays out a P2S loop file (structure as docs/sources.md documents it)."""
    top = max(bx[5] for bx in PLATE_BOXES)
    cooldown = ("; ================ FL NATURAL COOLDOWN ================\nM106 S0\nM106 P2 S0\n"
                + "G4 S30\n" * 240 + "; ================ FL NATURAL COOLDOWN END (120 min) ================\n")
    body = plate.replace("M109 S220\n", f"M109 S220\n{cooldown}", 1)        # the 2.72 bug: before the print
    push = top - 10                                                           # one height for the plate
    lanes = "".join(f"G1 Y250 F3000\nG1 X{x} F3000\nG1 Y0 F3000\n" for x in (220, 190, 160, 130, 100, 70, 30))
    end = ("; MACHINE_END_GCODE_START\nM400\nM140 S0 ; turn off bed\nM104 S0 ; turn off hotend\n"
           "M104 S2 ; FL_S2 T1 sentinel\n" + "M190 S26\n" * 30 +
           '; @fl:section:start "p2s.clear.sweep"\n'
           f"G1 Z{push:.2f} F10000\nM400\nG1 X125 F3000\n{lanes}"
           '; @fl:section:end "p2s.clear.sweep"\nM104 S5 ; FL_S2 T3 sentinel\nM73 P100 R0\n')
    a, b = body.index("; MACHINE_END_GCODE_START"), body.index("; EXECUTABLE_BLOCK_END")
    one = body[:a] + end + body[b:]
    return "".join((f"; === FL LOOP {k + 1} OF {loops} ===\n" if k else "") + one for k in range(loops))


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
    plate, plate_grams = bambu_plate()
    plate_bytes = plate_3mf(plate, plate_grams)
    (HERE / "bracket_plate.gcode.3mf").write_bytes(plate_bytes)
    (HERE / "FL_S2_farmloop_app.gcode.3mf").write_bytes(plate_3mf(farmloop_app_file(plate), plate_grams))
    name, data = loop_file(plate_bytes)
    (HERE / name).write_bytes(data)
    print("wrote", ", ".join(p.name for p in sorted(HERE.glob("*.gcode*"))))


if __name__ == "__main__":
    main()
