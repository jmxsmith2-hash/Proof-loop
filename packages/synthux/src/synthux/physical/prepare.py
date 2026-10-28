"""`synthux physical prepare`: from a CAD file to a print-ready verdict in one step.

1. The design check runs on the model (geometry, fit, people).
2. The model is sliced headlessly with Bambu Studio or OrcaSlicer, using the printer's own presets.
3. The sliced file is checked like any G-code (with what your printer taught, when ClearLoop is used).
4. The two are compared: what the design check flagged against what the slicer actually did. An
   overhang that needs support but got none, and a thin wall or detail the slicer left out, are found
   here and nowhere else.
5. Optionally, ClearLoop turns the sliced plate into a loop file, which is checked too.

Grams and time in the verdict are the slicer's own.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np

from synthux.core.config import ConfigError, Settings
from synthux.core.db import Database, utcnow
from synthux.physical.cad.load import load_profiles
from synthux.physical.slicer import PresetLibrary, find_slicer, slice_model, write_presets

SUPPORT_ON = {"enable_support": 1, "support_type": "tree(auto)"}
SUPPORT_OFF = {"enable_support": 0}


def _alignment(mesh, ctx) -> tuple[np.ndarray, str | None]:
    """Shift from model coordinates to the plate (the slicer centres the part and drops it to the bed)."""
    t = ctx.trace
    sel = ctx.extrude & ctx.model
    if not sel.any():
        return np.zeros(3), "the sliced file prints nothing"
    gx = np.concatenate([t.x0[sel], t.x1[sel]])
    gy = np.concatenate([t.y0[sel], t.y1[sel]])
    g_ext = np.array([np.ptp(gx), np.ptp(gy)])
    (x0, y0, z0), (x1, y1, _) = mesh.bounds
    c_ext = np.array([x1 - x0, y1 - y0])
    note = None
    if np.all(np.abs(g_ext - c_ext) > 3) and np.all(np.abs(g_ext - c_ext[::-1]) < 3):
        note = "the slicer turned the part, so the places below are approximate"
    shift = np.array([(gx.min() + gx.max()) / 2 - (x0 + x1) / 2, (gy.min() + gy.max()) / 2 - (y0 + y1) / 2, -z0])
    return shift, note


def _near(ctx, x: float, y: float, z_lo: float, z_hi: float, r: float, support: bool | None) -> int:
    """Extruding moves within r (mm) of (x, y), between two heights; support only, none, or all."""
    t = ctx.trace
    sel = ctx.extrude & ctx.model & (t.z >= z_lo) & (t.z <= z_hi)
    if support is not None:
        is_sup = np.array(["support" in name for name in t.features])[t.feature]
        sel &= is_sup if support else ~is_sup
    if not sel.any():
        return 0
    d0 = np.hypot(t.x0[sel] - x, t.y0[sel] - y)
    d1 = np.hypot(t.x1[sel] - x, t.y1[sel] - y)
    mx, my = (t.x0[sel] + t.x1[sel]) / 2 - x, (t.y0[sel] + t.y1[sel]) / 2 - y
    return int(((d0 <= r) | (d1 <= r) | (np.hypot(mx, my) <= r)).sum())


def compare(design: dict[str, Any], mesh, ctx) -> tuple[list[dict[str, Any]], str | None]:
    """What the design check flagged, against what the slicer did with it."""
    shift, note = _alignment(mesh, ctx)
    out: list[dict[str, Any]] = []
    support_used = any("support" in f for f in ctx.trace.features)
    for f in design["findings"]:
        loc = f.get("location")
        if not loc:
            continue
        x, y, z = loc["x"] + shift[0], loc["y"] + shift[1], loc["z"] + shift[2]
        r = max(float(loc.get("r", 0) or 0), 5.0)
        if f["check"] == "overhang" and f["severity"] >= 3:
            if not _near(ctx, x, y, 0.0, z, r, support=True):
                out.append({"check": "slicer_support", "severity": 3, "title": "No support under the overhang",
                            "detail": f"The design check found an overhang that needs support (#{f['n']}: {f['title']}); "
                                      "the sliced file prints no support under it, so it will droop or fail."
                                      + ("" if support_used else " Support is off in the process preset."),
                            "fix": "Turn support on (tree, auto) for this print (synthux physical prepare --support), "
                                   "or turn the part so the overhang faces up.", "design_finding": f["n"]})
        elif f["check"] in ("thin_wall", "fine_detail"):
            if not _near(ctx, x, y, max(0.0, z - 1.0), z + 1.0, 1.5, support=False):
                wall = f["check"] == "thin_wall"
                out.append({"check": "slicer_dropped", "severity": 4 if wall else 2,
                            "title": "The slicer left out a thin wall" if wall else "The slicer left out fine detail",
                            "detail": f"The design check found #{f['n']} ({f['title']}); the sliced file prints nothing "
                                      "there, because the slicer can't fit a line that narrow. It will be missing "
                                      "from the print.",
                            "fix": "Thicken it in the CAD model (see the design check's fix), or use a smaller nozzle.",
                            "design_finding": f["n"]})
    return out, note


def make_loop_file(sliced: Path, loops: int, out_dir: Path) -> Path:
    try:
        from clearloop import threemf as cl_threemf
        from clearloop.gcode.processor import process
        from clearloop.gcode.routines import LoopSettings
        from clearloop.profiles import ProfileStore
    except ImportError as exc:
        raise ConfigError('making a loop file needs ClearLoop: pip install "synthux[clearloop]"') from exc
    sf = cl_threemf.load(sliced)
    result = process(sf, ProfileStore().get("p2s-stage2-farmboard"), LoopSettings(loops=loops))
    path = out_dir / result.filename
    path.write_bytes(result.data)
    return path


def prepare(settings: Settings, db: Database, model: Path, *, printer: str | None = None,
            material: str | None = None, layer: float = 0.2, slicer: str | Path | None = None,
            process_preset: str | None = None, filament_preset: str | None = None, support: bool | None = None,
            loops: int = 0, out: Path | None = None, personas: int = 5, history=None,
            design: str | None = None, auto_eject: bool = False) -> dict[str, Any]:
    from synthux.personas.generate import PersonaGenerator
    from synthux.physical.cad.load import load_model
    from synthux.physical.cad.pipeline import check_design, design_spec
    from synthux.physical.cad.report import write_report
    from synthux.physical.gcode.pipeline import analyse, build_result

    model = Path(model)
    found = find_slicer(slicer)
    if found is None:
        raise ConfigError("no Bambu Studio or OrcaSlicer found. Install one, or pass --slicer with its path "
                          "(or set SYNTHUX_SLICER)")
    if not found.profile_dirs:
        raise ConfigError(f"found {found.label} at {found.exe} but not its printer profiles; set "
                          "SYNTHUX_SLICER_PROFILES to the folder that holds BBL/")
    pr, mat, _ = load_profiles(settings.config_dir, printer, material)
    out = Path(out) if out else settings.data_dir / "reports" / "prepare" / model.name.split(".")[0]
    out.mkdir(parents=True, exist_ok=True)

    lib = PresetLibrary(found.profile_dirs)
    machine = lib.machine((pr.get("names") or [pr["name"]])[0], float(pr["nozzle"]))
    proc = lib.process(machine, layer, process_preset)
    fil = lib.filament(machine, mat["name"], filament_preset)
    overrides = {} if support is None else {"process": SUPPORT_ON if support else SUPPORT_OFF}
    presets = write_presets(lib, out / "presets", machine, proc, fil, overrides)

    spec = design_spec(design, model)
    if auto_eject or loops:
        spec.use["auto_eject"] = True
    people = asyncio.run(PersonaGenerator(seed=11).generate(personas, include_accessibility=True))
    cad = check_design(settings, db, model, spec, people, out / "design", pr["name"], mat["name"])
    sliced, log = slice_model(found, model, presets, out / "sliced")
    ctx, notes, chosen = analyse(settings.config_dir, sliced, printer=pr["name"], material=mat["name"],
                                 history=history)
    gcode = build_result(ctx, notes, chosen, sliced, out=out / "gcode")
    mesh = load_model(model, spec.units).mesh
    slicer_findings, align_note = compare(cad, mesh, ctx)

    loop_result = None
    if loops:
        from synthux.physical.gcode.pipeline import check_gcode

        loop_path = make_loop_file(sliced, loops, out / "sliced")
        loop_result = check_gcode(settings.config_dir, loop_path, printer=pr["name"], material=mat["name"],
                                  out=out / "loop", history=history)
        loop_result["path"] = str(loop_path)

    blocking = [f for f in cad["findings"] if f["severity"] >= 3] + \
        [f for f in gcode["findings"] if f["severity"] >= 3] + [f for f in slicer_findings if f["severity"] >= 3]
    if loop_result:
        blocking += [f for f in loop_result["findings"] if f["severity"] >= 3 and f["check"] == "loop_file"]
    m = gcode["metrics"]
    result = {
        "model": model.name, "printer": pr, "material": mat, "slicer": {"name": found.label, "exe": str(found.exe)},
        "presets": {"machine": machine, "process": proc, "filament": fil, "overrides": overrides},
        "ready": not blocking, "blocking": len(blocking), "design": cad, "gcode": gcode,
        "slicer_findings": slicer_findings, "alignment_note": align_note, "loop": loop_result,
        "grams": m.get("filament_g"), "time": gcode["time"]["slicer"] or gcode["time"]["replay"],
        "auto_eject": cad["metrics"].get("auto_eject"),
        "layers": m.get("layers"), "sliced": str(sliced), "slicer_log": log[-4000:], "generated": utcnow(),
        "links": {"design": _rel(cad["report"], out), "gcode": _rel(gcode["report"], out),
                  "loop": _rel(loop_result["report"], out) if loop_result else None,
                  "sliced": _rel(str(sliced), out)},
    }
    result["report"] = str(write_report("prepare.html.j2", result, out))
    return result


def _rel(path: str, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return Path(path).as_uri()
