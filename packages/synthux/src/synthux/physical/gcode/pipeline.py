"""`synthux physical gcode`: parse, check, draw and report one sliced file."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from synthux.core.config import ConfigError, load_yaml_file
from synthux.physical.cad.load import load_profiles
from synthux.physical.gcode.checks import GcodeContext, run_gcode_checks, slicer_notes
from synthux.physical.gcode.parse import read_gcode
from synthux.physical.gcode.render import render_all


def printer_from_header(config_dir: Path, model: str | None) -> str | None:
    if not model:
        return None
    data = load_yaml_file(Path(config_dir) / "printers.yaml") or {}
    wanted = model.strip().lower()
    for name, spec in (data.get("printers") or {}).items():
        if any(wanted == n.lower() for n in spec.get("names", [])):
            return name
    return None


def _hours(seconds: float | None) -> str | None:
    if not seconds:
        return None
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def profiles_dir(config_dir: Path | str | None) -> Path:
    """The config folder with printers.yaml, or the copy shipped inside the package (for other tools)."""
    if config_dir and (Path(config_dir) / "printers.yaml").is_file():
        return Path(config_dir)
    return Path(__file__).resolve().parents[2] / "data"


def check_gcode(config_dir: Path | str | None, path: Path | str, *, printer: str | None = None,
                material: str | None = None, plate: int | None = None, out: Path | None = None,
                write_report: bool = True) -> dict[str, Any]:
    """Check one sliced file. With write_report=False nothing is written (for tools that show results)."""
    started = time.perf_counter()
    config_dir = profiles_dir(config_dir)
    path = Path(path)
    trace = read_gcode(path, plate)
    notes = slicer_notes(trace.header, trace.plate_info)
    detected = printer_from_header(config_dir, notes.get("printer"))
    printer_name = printer or detected
    pr, mat, slicer = load_profiles(config_dir, printer_name, material or notes.get("filament_type"))
    if pr.get("type") == "resin":
        raise ConfigError("resin printers don't use G-code; check the model with `synthux physical check`")
    pr["label"] = (pr.get("names") or [pr["name"]])[0]
    nozzle = float(notes.get("nozzle") or pr["nozzle"])
    ctx = GcodeContext(trace=trace, printer=pr, material=mat, nozzle=nozzle,
                       filament_diameter=float(pr.get("filament_diameter", slicer.get("filament_diameter", 1.75))))
    run_gcode_checks(ctx, notes)
    images: dict[str, str] = {}
    if write_report:
        out = Path(out) if out else Path("reports") / "gcode" / Path(path).name.split(".")[0]
        out.mkdir(parents=True, exist_ok=True)
        images = render_all(ctx, out)
    m = ctx.metrics
    findings = []
    for n, f in enumerate(ctx.findings, 1):
        findings.append({"n": n, "check": f.check, "severity": f.severity, "severity_name": f.severity_name,
                         "title": f.title, "detail": f.detail, "fix": f.fix, "location": f.location,
                         "layer": f.data.get("layer"), "loop_no": f.data.get("loop_no"),
                         "layer_in_loop": f.data.get("layer_in_loop"),
                         "image": images.get(f.data.get("image_key") or "")})
    result = {
        "file": trace.source, "printer": pr, "material": mat, "nozzle": nozzle,
        "printer_source": "chosen" if printer else ("from the file" if detected else "default"),
        "material_source": "chosen" if material else ("from the file" if notes.get("filament_type") else "default"),
        "notes": notes, "findings": findings, "metrics": m, "images": images,
        "plates": trace.plate_info.get("plates"), "plate": trace.plate,
        "time": {"slicer": _hours(m.get("slicer_time_s")), "replay": _hours(m.get("replay_time_s"))},
        "cost": round(m.get("filament_g", 0) / 1000 * float(mat.get("price_per_kg", 0)), 2),
        "lines": trace.lines, "moves": len(trace),
        "seconds": round(time.perf_counter() - started, 1),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    if write_report:
        from synthux.physical.cad.report import write_report

        result["report"] = str(write_report("gcode.html.j2", result, out))
    return result
