"""The results of a check in one compact, stable shape: for ``--json`` and for assistants (``proofloop mcp``).

Every finding has an ``id`` that names the check that found it (``stability``, ``overhang``,
``first_layer_temp``...) and stays the same between versions and runs, a ``severity`` (1 note,
2 minor, 3 major, 4 will fail), what to change (``fix``) and, when there is one, where it is
(``location`` in mm, or ``layer``) and a picture of it (``image``, a file path). ``ready`` is true
when nothing major or worse is left. Paths are absolute.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from proofloop.core.output import BLOCKING

SCHEMA = 1


def _path(folder: Path, rel: str | None) -> str | None:
    if not rel:
        return None
    p = Path(rel)
    return str(p if p.is_absolute() else (folder / p).resolve())


def finding(f: dict[str, Any], folder: Path) -> dict[str, Any]:
    out = {
        "id": f.get("check"),
        "n": f.get("n"),
        "severity": f.get("severity"),
        "level": f.get("severity_name"),
        "title": f.get("title"),
        "detail": f.get("detail"),
        "fix": f.get("fix"),
    }
    for key in ("location", "layer", "loop_no", "fingerprint"):
        if f.get(key) not in (None, {}):
            out[key] = f[key]
    if f.get("settings"):
        out["slicer_settings"] = f["settings"]
    if f.get("image"):
        out["image"] = _path(folder, f["image"])
    return out


def _verdict(findings: list[dict]) -> dict[str, Any]:
    blocking = sum(1 for f in findings if (f.get("severity") or 0) >= BLOCKING)
    return {"ready": blocking == 0, "blocking": blocking}


def design(r: dict[str, Any]) -> dict[str, Any]:
    folder = Path(r["report"]).parent
    made = r["process"]
    printer = (r["printer"].get("names") or [r["printer"].get("label") or r["printer"].get("name")])[0]
    c = r.get("comparison") or None
    return {
        "schema": SCHEMA,
        "kind": "design",
        "design": r["design"],
        "version": r["version"],
        "file": str(r["file"]),
        **_verdict(r["findings"]),
        "process": made["kind"],
        "printer": printer if made["kind"] == "print" else made.get("label"),
        "material": r["material"]["name"],
        "size_mm": r["metrics"].get("size_mm"),
        "estimates": {k: v for k, v in r["metrics"].get("estimates", {}).items() if k != "process_stats"},
        "findings": [finding(f, folder) for f in r["findings"]],
        "since_last_version": {
            "version": c["previous_version"],
            "fixed": [x.get("title") if isinstance(x, dict) else x for x in c.get("fixed", [])],
            "still_there": [x.get("title") if isinstance(x, dict) else x for x in c.get("persisting", [])],
            "new": [x.get("title") if isinstance(x, dict) else x for x in c.get("new", [])],
        } if c else None,
        "renders": {k: _path(folder, v) for k, v in (r.get("images") or {}).items()},
        "report": str(Path(r["report"]).resolve()),
    }


def gcode(r: dict[str, Any]) -> dict[str, Any]:
    folder = Path(r["report"]).parent
    m = r.get("metrics") or {}
    lf = m.get("loop_file")
    return {
        "schema": SCHEMA,
        "kind": "gcode",
        "file": str(r["file"]),
        **_verdict(r["findings"]),
        "printer": r["printer"].get("label"),
        "material": r["material"].get("name"),
        "nozzle_mm": r.get("nozzle"),
        "layers": m.get("layers"),
        "filament_g": m.get("filament_g"),
        "time": r["time"].get("slicer") or r["time"].get("replay"),
        "loop_file": {"made_by": lf["made_by"], "loops": lf["loops"], "plate_check_pauses": lf["plate_check_pauses"],
                      "filament_g_per_loop": m.get("filament_g_per_loop")} if lf else None,
        "findings": [finding(f, folder) for f in r["findings"]],
        "slicer_settings": r.get("setting_fixes") or [],
        "report": str(Path(r["report"]).resolve()),
    }


def prepare(r: dict[str, Any]) -> dict[str, Any]:
    folder = Path(r["report"]).parent
    findings = ([finding(f, folder) | {"from": "slicer"} for f in r.get("slicer_findings", [])]
                + [finding(f, folder) | {"from": "design"} for f in r["design"]["findings"]]
                + [finding(f, folder) | {"from": "gcode"} for f in r["gcode"]["findings"]])
    return {
        "schema": SCHEMA,
        "kind": "prepare",
        "ready": bool(r["ready"]),
        "blocking": r.get("blocking", 0),
        "slicer": r["slicer"]["name"],
        "presets": r["presets"],
        "grams": r.get("grams"),
        "time": r.get("time"),
        "layers": r.get("layers"),
        "rounds": [{"applied": rd["applied"], "blocking_before": rd["blocking_before"],
                    "blocking_after": rd["blocking_after"]} for rd in r.get("rounds", [])],
        "findings": findings,
        "slicer_settings": r.get("setting_fixes") or [],
        "loop_file": (r.get("loop") or {}).get("path"),
        "report": str(Path(r["report"]).resolve()),
    }
