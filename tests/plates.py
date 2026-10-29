"""Synthetic plates with the same structure as real Bambu Studio P2S slices.

The layout (header/config/executable blocks, start template header and date
line, object labels, stock end G-code ending in M18, slice_info with
printer_model_id) mirrors the public P2S evidence files the built-in profile
was learned from. The FarmLoop-style file reuses the built-in profile's own
vendor blocks, so importer tests run against the real gestures.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

from proofloop.loopfiles.profiles import ProfileStore

START_TEMPLATE = """;M1002 set_flag extrude_cali_flag=1

;======== P2S start gcode==========
;===== 2026/05/18 =====

  M140 S55 ; heat heatbed first
  M400
G28
M109 S220
"""

END_TEMPLATE = """;======== P2S end gcode ==========
;===== 2026/05/18 =====
M400 ; wait for buffer to clear
G92 E0 ; zero the extruder
M211 Z1
G90
G1 Z{z} F900 ; lower z a little
M140 S0 ; turn off bed
M106 S0 ; turn off fan
M106 P2 S0 ; turn off remote part cooling fan
M106 P3 S0 ; turn off chamber cooling fan
M106 P10 S0 ; turn off left aux fan
M104 S0 ; turn off hotend
M400 ; wait all motion done
M17 S
M17 Z0.4 ; lower z motor current to reduce impact if there is something in the bottom
G1 Z{z2} F600
M400 P100
M17 R ; restore z current
M400
M18
"""


def plate_gcode(boxes, layer=0.2, model_id="N7", printer_model="Bambu Lab P2S", filament="PLA", end_m18=True):
    """boxes: list of (label, x0, y0, x1, y1, height)."""
    top = max(b[5] for b in boxes)
    labels = ",".join(b[0] for b in boxes)
    heights = ",".join(f"{b[5]:.2f}" for b in boxes)
    out = [
        "; HEADER_BLOCK_START",
        "; BambuStudio 02.07.01.62",
        "; model printing time: 1h 0m 0s; total estimated time: 1h 2m 3s",
        f"; total layer number: {int(round(top / layer))}",
        f"; model label id: {labels}",
        f"; object max height: {heights}",
        f"; max_z_height: {top:.2f}",
        "; HEADER_BLOCK_END",
        "",
        "; CONFIG_BLOCK_START",
        f"; printer_model = {printer_model}",
        "; curr_bed_type = Textured PEI Plate",
        "; printable_area = 0x0,256x0,256x256,0x256",
        "; printable_height = 256",
        f"; filament_type = {filament}",
        '; filament_settings_id = "Bambu PLA Basic @BBL P2S"',
        '; filament_vendor = "Bambu Lab"',
        "; filament_density = 1.26",
        "; filament_diameter = 1.75",
        "; filament_cost = 24.99",
        "; filament_dev_ams_drying_temperature = 45,45,45,45",
        "; filament_dev_ams_drying_time = 12,12,12,12",
        "; machine_max_speed_x = 1000,1000",
        "; machine_max_speed_z = 20,20",
        "; CONFIG_BLOCK_END",
        "",
        "; EXECUTABLE_BLOCK_START",
        "M73 P0 R62",
        "M106 S0",
        START_TEMPLATE.rstrip("\n"),
        "; purge line, not an object",
        "G1 X10 Y1 Z0.3 F3000",
        "G1 X120 Y1 E8 F1500",
        "; MACHINE_START_GCODE_END",
        "G90",
        "M83",
    ]
    z = 0.0
    n = 0
    while z < top - 1e-9:
        z = round(z + layer, 3)
        n += 1
        out += [f"G1 Z{z} F600", f"M73 L{n}"]
        for label, x0, y0, x1, y1, h in boxes:
            if z > h + 1e-9:
                continue
            out += [
                f"; start printing object, unique label id: {label}",
                f"G1 X{x0} Y{y0} F12000",
                f"G1 X{x1} Y{y0} E1",
                f"G1 X{x1} Y{y1} E1",
                f"G1 X{x0} Y{y1} E1",
                f"G1 X{x0} Y{y0} E1",
                f"; stop printing object, unique label id: {label}",
            ]
    end = END_TEMPLATE.format(z=top + 10, z2=top + 4)
    if not end_m18:
        end = end.replace("M18\n", "")
    out += ["; MACHINE_END_GCODE_START", end.rstrip("\n"), "M73 P100 R0", "; EXECUTABLE_BLOCK_END", "", ""]
    return "\n".join(out)


def make_3mf(gcode: str, boxes=None, model_id="N7", filament="PLA", grams=12.5) -> bytes:
    objects = "".join(
        f'<object identify_id="{b[0]}" name="Part {b[0]}" skipped="false" />' for b in (boxes or [])
    )
    slice_info = (
        '<?xml version="1.0" encoding="UTF-8"?><config><plate>'
        f'<metadata key="index" value="1"/><metadata key="printer_model_id" value="{model_id}"/>'
        '<metadata key="prediction" value="3723"/>'
        f"{objects}"
        f'<filament id="1" type="{filament}" color="#FFFFFF" used_m="1" used_g="{grams}"/>'
        "</plate></config>"
    )
    data = gcode.encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", "<model/>")
        z.writestr("Metadata/plate_1.gcode", data)
        z.writestr("Metadata/plate_1.gcode.md5", hashlib.md5(data).hexdigest().upper())
        z.writestr("Metadata/slice_info.config", slice_info)
        z.writestr("Metadata/plate_1.png", b"\x89PNG")
    return buf.getvalue()


BUCKET = [("93", 94.0, 107.0, 162.0, 162.0, 170.0), ("104", 124.6, 94.0, 136.8, 106.5, 5.0)]
SMALL = [("1", 100.0, 100.0, 140.0, 140.0, 12.0), ("2", 170.0, 60.0, 190.0, 80.0, 4.0)]


def plain(boxes=SMALL, **kw) -> bytes:
    return make_3mf(
        plate_gcode(boxes, **kw), boxes, model_id=kw.get("model_id", "N7"), filament=kw.get("filament", "PLA")
    )


def farmloop_version(gcode: str, stage: int = 2, natural_cooldown: bool = True) -> str:
    """Process a plain plate the way the FarmLoop app 2.72 does for a P2S (structure from the public evidence)."""
    p = ProfileStore().get("p2s-stage2-farmboard")
    lines = gcode.splitlines(keepends=True)
    date_i = next(
        i for i, ln in enumerate(lines) if ln.startswith(";===== 2026/05/18") and i < 40 + len(START_TEMPLATE)
    )
    if stage == 2:
        lines.insert(date_i + 1, p.start_block)
    if natural_cooldown:
        heat = next(i for i, ln in enumerate(lines) if ln.startswith("M109 S220"))
        nat = (
            "; ================ FL NATURAL COOLDOWN ================\nM106 S0\nM106 P2 S0\nM106 P3 S0\n"
            + "G4 S30\n" * 240
        )
        nat += "; ================ FL NATURAL COOLDOWN END (120 min) ================\n"
        lines.insert(heat, nat)
    text = "".join(lines)
    end_start = text.index("; MACHINE_END_GCODE_START")
    exec_end = text.index("; EXECUTABLE_BLOCK_END")
    sweep = (
        '; @fl:section:start "p2s.clear.sweep"\n;============================= PUSH SECTION =============================\n'
        '; @fl:replace next 1 "clear-height.smart-default"\nG1 Z160.00 F10000\nM400\n'
        "G1 X125 F3000\nG1 Y250 F3000\nG1 Y0   F3000\nG1 Y250 F3000\nG1 Y0   F3000\n"
        + "".join(f"G1 Y250 F3000\nG1 X{x} F3000\nG1 Y0   F3000\n" for x in (220, 190, 160, 130, 100, 70, 30))
        + "G1 X220 F3000\nM400\nG1 Z160.00 F12000\nM400\n"
        + "".join(
            f"G1 Y250 F12000\nG1 X{x} F12000\nG1 Y0   F12000\n" for x in (220, 190, 160, 130, 100, 70, 30)
        )
        + ";===============================  END SECTION  =============================\n\nM73 P74 R2\n"
        "G1 X65 Y245 F12000     ; move to safe corner before parking\nG1 Y265 F3000          ; final park at rear edge\n"
    )
    if stage == 2:
        end = (
            "; MACHINE_END_GCODE_START\n;======== P2S end gcode ==========\nM400\nM140 S0 ; turn off bed\nM104 S0 ; turn off hotend\n"
            "M106 P2 S255 ; turn on remote part cooling fan\n"
            + p.door_open
            + ";Cooldown Start\n"
            + "M190 S26\n" * 60
            + ";Cooldown End\nM140 S0 ; turn off bed\nM104 S0 ; turn off hotend\n\n"
            + p.bend
            + "\n"
            + sweep
            + "\n"
            + p.safety
            + "\n"
            + p.door_close.replace("M104 S5", "M104 S5", 1)
            + '; @fl:section:end "p2s.clear.sweep"\n'
            "\nM106 S0 ; turn off fan\nM106 P2 S0 ; turn off remote part cooling fan\nM106 P3 S0 ; turn off chamber cooling fan\nM73 P100 R0\n"
        )
    else:
        end = (
            "; MACHINE_END_GCODE_START\nM400\nM140 S0\nM104 S0\nM106 P2 S255\n;Cooldown Start\n"
            + "M190 S30\n" * 10
            + ";Cooldown End\nM140 S0\nM104 S0\n; static bend\nG1 Z250 F3000\nM400\nG4 S3\nG1 Z200 F3000\n"
            + sweep
            + "\nM106 S0\nM106 P2 S0\nM73 P100 R0\n"
        )
    return text[:end_start] + end + text[exec_end:]


def farmloop_3mf(boxes=SMALL, stage=2, natural_cooldown=True) -> bytes:
    return make_3mf(farmloop_version(plate_gcode(boxes), stage, natural_cooldown), boxes)
