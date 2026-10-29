"""Print checks for a loop file: how well each loop's print itself will come out.

The loop-file checks (``lint.py``) look at the ejection cycle and the file's structure. The G-code
check (``proofloop.check.gcode``) also looks at the print in each loop: parts off the plate, first
layer and layer heights, nozzle and bed temperatures for the filament, flow the hot end can't melt,
retraction and stringing, cooling and warping. It reads only the print in each loop, not the
cooldown or sweep added after it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from proofloop.loopfiles.lint import Finding
from proofloop.loopfiles.threemf import SlicedFile

# G-code check severities: 4 will fail, 3 major, 2 minor, 1 note
LEVELS = {4: "danger", 3: "warning", 2: "warning", 1: "info"}


def print_checks(sf: SlicedFile) -> list[Finding]:
    """Print-quality findings for the file."""
    from proofloop.check.gcode import check_file

    with tempfile.TemporaryDirectory(prefix="proofloop-print-") as tmp:
        path = Path(tmp) / (Path(sf.name).name or "plate.gcode.3mf")
        if not path.name.lower().endswith(".3mf"):
            path = path.with_name(path.name + ".gcode.3mf")
        path.write_bytes(sf.to_bytes(sf.gcode))
        try:
            result = check_file(path, plate=sf.plate, write_report=False)
        except Exception as exc:  # noqa: BLE001 - print checks must never hide the loop-file checks
            return [Finding("info", "Print checks couldn't read this file", str(exc))]
    out = []
    for f in result["findings"]:
        if f["check"] == "loop_file":
            continue  # the ejection cycle: the loop-file checks already cover it
        where = ""
        if f.get("loop_no"):
            where = f" (loop {f['loop_no']}, layer {f['layer_in_loop']})"
        elif f.get("layer"):
            where = f" (layer {f['layer']})"
        detail = f["detail"] + (f" Fix: {f['fix']}" if f.get("fix") else "")
        out.append(Finding(LEVELS.get(f["severity"], "info"), f"Print: {f['title']}{where}", detail))
    if not out:
        m = result["metrics"]
        grams = m.get("filament_g_per_loop") or m.get("filament_g")
        out.append(
            Finding(
                "ok",
                "Print checks passed",
                f"{m.get('layers', 0)} layers and {grams} g per loop on a {result['printer']['label']} in "
                f"{result['material']['name']}: plate, layers, temperatures, flow, retraction and cooling look right.",
            )
        )
    return out
