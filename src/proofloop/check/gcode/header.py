"""The file against its own slicer header: filament totals, print time, printer and nozzle."""

from __future__ import annotations

import re
from typing import Any

from proofloop.check.gcode.context import GcodeContext


def check_consistency(ctx: GcodeContext, slicer_mm: float | None, slicer_g: float | None, density: float) -> None:
    t = ctx.trace
    used = float(t.de.sum())
    grams = used * ctx.fil_area / 1000 * density
    ctx.metrics["filament_m"] = round(used / 1000, 2)
    ctx.metrics["filament_g"] = round(grams, 1)
    loops = ctx.metrics.get("loops", 1)
    if loops > 1:
        # a loop file carries one slicer header per loop; compare the whole file with all of them
        ctx.metrics["filament_g_per_loop"] = round(grams / loops, 1)
        slicer_mm = _sum_header(t.header_all, ("total filament length [mm]", "filament used [mm]"), loops) or slicer_mm
        slicer_g = _sum_header(t.header_all, ("total filament weight [g]", "filament used [g]"), loops) or (
            slicer_g * loops if slicer_g else None)
    for label, theirs, ours, unit in (("filament length", slicer_mm, used, "mm"), ("weight", slicer_g, grams, "g")):
        if theirs and ours > 0 and abs(theirs - ours) / max(theirs, ours) > 0.15:
            ctx.add("consistency", 2, "The file doesn't match its own header",
                    f"The slicer's header says {theirs:.0f} {unit} of {label}; the moves in the file use {ours:.0f} {unit}. "
                    "The file may have been edited after slicing, cut short, or the header belongs to another plate.",
                    "Re-slice and export again if you didn't edit it on purpose.")
            return


def _sum_header(values: dict[str, list[str]], keys: tuple[str, ...], loops: int) -> float | None:
    for key in keys:
        found = [v for v in values.get(key, []) if re.search(r"\d", v)]
        if not found:
            continue
        nums = [float(m.group()) for m in (re.search(r"-?\d+(\.\d+)?", v.split(",")[0]) for v in found) if m]
        if not nums:
            continue
        return sum(nums) if len(nums) == loops else nums[0] * loops
    return None


def parse_duration(text: str | None) -> float | None:
    if not text:
        return None
    text = str(text)
    if re.fullmatch(r"\s*\d+(\.\d+)?\s*", text):
        return float(text)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", text)
    if not parts:
        return None
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(float(v) * mult[u] for v, u in parts)


def slicer_notes(header: dict[str, str], plate_info: dict[str, Any]) -> dict[str, Any]:
    def first(*keys: str) -> str | None:
        for k in keys:
            if header.get(k):
                return header[k].strip().strip('"')
        return None

    def number(text: str | None) -> float | None:
        if not text:
            return None
        m = re.search(r"-?\d+(\.\d+)?", text.split(",")[0].split(";")[0])
        return float(m.group()) if m else None

    est = None
    combined = header.get("model printing time", "")
    if "total estimated time" in combined:
        est = parse_duration(combined.split("total estimated time", 1)[1])
    est = est or parse_duration(first("total estimated time", "estimated printing time (normal mode)",
                                      "estimated printing time")) or parse_duration(first("time"))
    est = est or plate_info.get("prediction_s")
    length = number(first("total filament length [mm]", "filament used [mm]"))
    cura_m = first("filament used")
    if length is None and cura_m and cura_m.strip().endswith("m"):
        cura_len = number(cura_m)
        length = cura_len * 1000 if cura_len is not None else None
    filament_type = first("filament_type")
    if not filament_type and plate_info.get("filaments"):
        filament_type = plate_info["filaments"][0].get("type")
    return {
        "printer": first("printer_model", "printer_settings_id", "machine_name"),
        "nozzle": number(first("nozzle_diameter", "extruder_train.0.nozzle.diameter")) or
        number(str(plate_info.get("nozzle_diameters") or "")),
        "filament_type": filament_type.split(";")[0].strip() if filament_type else None,
        "filament_diameter": number(first("filament_diameter")),
        "time_s": est,
        "filament_mm": length,
        "filament_g": number(first("total filament weight [g]", "filament used [g]")) or plate_info.get("weight_g"),
        "slicer": next((ln for ln in header if ln.lower().startswith(("bambustudio", "orcaslicer", "prusaslicer"))), None),
    }


def loop_times(header_all: dict[str, list[str]]) -> list[float]:
    """Slicer time of each loop's print, from each loop's own header."""
    out = []
    for v in header_all.get("model printing time", []):
        tail = v.split("total estimated time", 1)
        sec = parse_duration(tail[1] if len(tail) > 1 else v)
        if sec:
            out.append(sec)
    return out
