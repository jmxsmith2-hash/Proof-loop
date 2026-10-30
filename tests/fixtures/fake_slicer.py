"""A stand-in for Bambu Studio's command line, for tests (the real slicer can't run in CI).

It takes the same options (--slice --export-3mf --load-settings --load-filaments --arrange --orient
--outputdir model) and writes a .gcode.3mf in Bambu Studio's layout. Like a real slicer it cuts the
model into layers, leaves out anything narrower than the nozzle, and adds support under overhangs
only when the process preset has enable_support on. Lines are straight passes across each layer.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sys
import zipfile
from pathlib import Path

import numpy as np
import trimesh
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from proofloop.check.cad.detail import _disc, section_mask  # noqa: E402

FIL_AREA = math.pi * (1.75 / 2) ** 2


def first(v, default=None):
    if isinstance(v, list):
        return v[0] if v else default
    return v if v is not None else default


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice")
    ap.add_argument("--export-3mf", dest="out")
    ap.add_argument("--load-settings", dest="settings")
    ap.add_argument("--load-filaments", dest="filaments")
    ap.add_argument("--arrange")
    ap.add_argument("--orient")
    ap.add_argument("--outputdir")
    ap.add_argument("model")
    a = ap.parse_args(argv)
    machine, process = (json.loads(Path(p).read_text(encoding="utf-8")) for p in a.settings.split(";"))
    filament = json.loads(Path(a.filaments.split(";")[0]).read_text(encoding="utf-8"))
    layer = float(first(process.get("layer_height"), 0.2))
    nozzle = float(first(machine.get("nozzle_diameter"), 0.4))
    support = str(first(process.get("enable_support"), "0")) == "1"
    noz_c = int(float(first(filament.get("nozzle_temperature"), 220)))
    bed_c = int(float(first(filament.get("hot_plate_temp"), 55)))
    mesh = trimesh.load(a.model, force="mesh")
    mesh.apply_translation([128 - mesh.bounds[:, 0].mean(), 128 - mesh.bounds[:, 1].mean(), -mesh.bounds[0, 2]])
    pitch = nozzle / 4
    (x0, y0, _), (x1, y1, top) = mesh.bounds
    origin = np.array([x0 - 4, y0 - 4])
    shape = (int((y1 - y0 + 8) / pitch) + 1, int((x1 - x0 + 8) / pitch) + 1)
    zs = [layer]
    while zs[-1] + layer <= top + 1e-6:
        zs.append(round(zs[-1] + layer, 3))
    masks = [ndimage.binary_opening(section_mask(mesh, z - layer / 2, origin, pitch, shape), _disc(nozzle / 2 / pitch))
             for z in zs]                              # what a nozzle-wide line can trace
    above = np.zeros(shape, bool)
    supports = [None] * len(zs)
    for k in range(len(zs) - 1, -1, -1):              # support: under anything higher, where this layer is empty
        if support:
            supports[k] = above & ~ndimage.binary_dilation(masks[k], iterations=2)
        above |= masks[k]
    body, total = [], 0.0
    for k, z in enumerate(zs):
        body += ["; CHANGE_LAYER", f"; Z_HEIGHT: {z:g}", f"; LAYER_HEIGHT: {layer:g}"]
        if k == 1:
            body.append("M106 P1 S255")
        for feature, m in (("Internal solid infill", masks[k]), ("Support", supports[k])):
            if m is None or not m.any():
                continue
            if feature == "Internal solid infill":
                body.append("; start printing object, unique label id: 1")
            body.append(f"; FEATURE: {feature}")
            for r in range(0, shape[0], max(1, round(0.45 / pitch))):
                row = np.concatenate([[0], m[r].astype(int), [0]])
                edges = np.flatnonzero(np.diff(row))
                y = origin[1] + (r + 0.5) * pitch
                for s, e in zip(edges[::2], edges[1::2], strict=False):
                    xs, xe = origin[0] + s * pitch, origin[0] + e * pitch
                    if xe - xs < nozzle:
                        continue
                    ext = (xe - xs) * 0.45 * layer / FIL_AREA * (0.6 if feature == "Support" else 1.0)
                    total += ext
                    body += ["G1 E-0.8 F2100", f"G1 X{xs:.3f} Y{y:.3f} Z{z:.3f} F30000", "G1 E0.8 F2100",
                             f"G1 X{xe:.3f} Y{y:.3f} E{ext:.5f} F9000"]
            if feature == "Internal solid infill":
                body.append("; stop printing object, unique label id: 1")
    grams = total * FIL_AREA / 1000 * float(first(filament.get("filament_density"), 1.24))
    head = ["; HEADER_BLOCK_START", "; BambuStudio 02.02.00.00 (ProofLoop test stand-in)",
            "; model printing time: 30m 0s; total estimated time: 33m 0s", f"; total layer number: {len(zs)}",
            f"; total filament length [mm] : {total:.2f}", f"; total filament weight [g] : {grams:.2f}",
            "; model label id: 1", f"; object max height: {top:.2f}", f"; max_z_height: {top:.2f}",
            "; HEADER_BLOCK_END", "", "; CONFIG_BLOCK_START",
            f"; printer_model = {first(machine.get('printer_model'), 'Bambu Lab P2S')}",
            f"; nozzle_diameter = {nozzle}", f"; filament_type = {first(filament.get('filament_type'), 'PLA')}",
            f"; filament_settings_id = \"{filament.get('name', '')}\"", f"; layer_height = {layer}",
            f"; enable_support = {int(support)}", "; filament_diameter = 1.75",
            f"; printable_area = {','.join(machine.get('printable_area') or ['0x0', '256x0', '256x256', '0x256'])}",
            f"; printable_height = {first(machine.get('printable_height'), 256)}",
            "; curr_bed_type = Textured PEI Plate", "; CONFIG_BLOCK_END", "",
            "; EXECUTABLE_BLOCK_START",
            # like the real slicer: the printer preset's own start G-code, with temperatures filled in
            *str(machine.get("machine_start_gcode") or "G28").replace("[bed_temperature_initial_layer_single]",
                                                                      str(bed_c))
            .replace("[nozzle_temperature_initial_layer]", str(noz_c)).splitlines(),
            "G90", "M83", f"M140 S{bed_c}", f"M104 S{noz_c}", f"M190 S{bed_c}", f"M109 S{noz_c}",
            "; MACHINE_START_GCODE_END"]
    end = str(machine.get("machine_end_gcode") or "M400\nM140 S0\nM104 S0\nM106 S0").splitlines()
    tail = ["; MACHINE_END_GCODE_START", *end, "; EXECUTABLE_BLOCK_END", ""]
    data = "\n".join(head + body + tail).encode()
    info = ('<?xml version="1.0" encoding="UTF-8"?><config><plate><metadata key="index" value="1"/>'
            '<metadata key="prediction" value="1980"/>'  # like the real command line: no printer_model_id
            f'<metadata key="weight" value="{grams:.2f}"/><object identify_id="1" name="{Path(a.model).stem}" />'
            f'<filament id="1" type="{first(filament.get("filament_type"), "PLA")}" color="#FFFFFF" used_m="1" '
            f'used_g="{grams:.2f}"/></plate></config>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/plate_1.gcode", data)
        zf.writestr("Metadata/plate_1.gcode.md5", hashlib.md5(data).hexdigest().upper())
        zf.writestr("Metadata/slice_info.config", info)
    # Like Bambu Studio and Orca: the export name goes inside --outputdir (an absolute path fails, -13)
    if Path(a.out).is_absolute():
        (Path(a.outputdir) / "result.json").write_text(json.dumps(
            {"return_code": -13, "error_string": "Failed exporting 3mf files."}), encoding="utf-8")
        return -13 & 0xFF
    (Path(a.outputdir) / a.out).write_bytes(buf.getvalue())
    print(f"sliced {a.model}: {len(zs)} layers, support {'on' if support else 'off'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
