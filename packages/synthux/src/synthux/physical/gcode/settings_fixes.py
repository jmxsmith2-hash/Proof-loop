"""Turn G-code findings into the slicer settings that fix them (Bambu Studio / OrcaSlicer keys).

Each finding a slicer setting can fix gets the exact preset key and value, with where to find it in
the slicer. The keys are the ones in Bambu Studio's presets and PrintConfig.cpp (OrcaSlicer shares
them). `user_presets` writes them as user presets that inherit from the presets the file was sliced
with, ready for File > Import > Import Configs; `synthux physical prepare --fix` applies them and
slices again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

WHERE = {
    "filament_max_volumetric_speed": "Filament › Max volumetric speed",
    "nozzle_temperature": "Filament › Nozzle temperature (other layers)",
    "nozzle_temperature_initial_layer": "Filament › Nozzle temperature (initial layer)",
    "fan_min_speed": "Filament › Cooling › Fan speed (minimum)",
    "fan_max_speed": "Filament › Cooling › Fan speed (maximum)",
    "slow_down_for_layer_cooling": "Filament › Cooling › Slow printing down for better layer cooling",
    "slow_down_layer_time": "Filament › Cooling › Layer time",
    "filament_retraction_length": "Filament › Setting overrides › Retraction length",
    "filament_diameter": "Filament › Diameter",
    "initial_layer_print_height": "Process › Quality › Initial layer height",
    "layer_height": "Process › Quality › Layer height",
    "initial_layer_speed": "Process › Speed › Initial layer",
    "initial_layer_infill_speed": "Process › Speed › Initial layer infill",
    "brim_type": "Process › Others › Brim type",
    "brim_width": "Process › Others › Brim width",
    "enable_support": "Process › Support › Enable support",
    "support_type": "Process › Support › Type",
    "wall_generator": "Process › Quality › Wall generator",
}
PLATE_KEYS = {"textured": "textured_plate_temp", "cool": "cool_plate_temp", "engineering": "eng_plate_temp",
              "high temp": "hot_plate_temp", "smooth": "hot_plate_temp"}
FILAMENT_KEYS = {k for k in WHERE if WHERE[k].startswith("Filament")} | set(PLATE_KEYS.values()) | \
    {v + "_initial_layer" for v in PLATE_KEYS.values()}
LIST_PROCESS_KEYS = {"initial_layer_speed", "initial_layer_infill_speed"}


def _fix(preset: str, key: str, value: Any, why: str) -> dict[str, Any]:
    label = WHERE.get(key) or ("Filament › Bed temperature (" + key.replace("_", " ") + ")")
    return {"preset": preset, "key": key, "value": str(value), "label": label, "why": why}


def _plate_keys(header: dict[str, str]) -> list[str]:
    bed = (header.get("curr_bed_type") or "").lower()
    for word, key in PLATE_KEYS.items():
        if word in bed:
            return [key]
    return ["textured_plate_temp", "hot_plate_temp"]       # the P2S's usual plates


def fixes_for(ctx: Any) -> None:
    """Attach `settings` (a list of changes) to each finding a slicer setting fixes."""
    mat, noz = ctx.material, ctx.nozzle
    lo, hi = (mat.get("nozzle_c") or [None, None])
    blo, bhi = (mat.get("bed_c") or [None, None])
    hot = float(ctx.printer.get("max_flow_mm3_s") or 0) or None
    mflow = float(mat.get("flow_mm3_s") or 0) or None
    for f in ctx.findings:
        t, c = f.title, f.check
        out: list[dict[str, Any]] = []
        if (c == "flow" or (c == "history" and "flow" in t)) and "prints it fine" not in t:
            failed = (ctx.history or {}).get("failed_flow")
            limits = [x for x in (hot, mflow, 0.85 * failed if failed else None) if x]
            if limits:
                out.append(_fix("filament", "filament_max_volumetric_speed", round(min(limits)), t))
        elif c == "first_layer" and "thick" in t:
            out.append(_fix("process", "initial_layer_print_height", round(min(0.3, 0.6 * noz), 2), t))
        elif c == "first_layer" and "Fast" in t:
            out += [_fix("process", "initial_layer_speed", 50, t), _fix("process", "initial_layer_infill_speed", 80, t)]
        elif c == "layer_height" and "thick" in t:
            out.append(_fix("process", "layer_height", round(0.75 * noz, 2), t))
        elif c == "temperature" and "Nozzle" in t and lo is not None and ("cold" in t or "hot" in t):
            target = round((lo + hi) / 2 / 5) * 5
            out += [_fix("filament", "nozzle_temperature", target, t),
                    _fix("filament", "nozzle_temperature_initial_layer", target, t)]
        elif c == "temperature" and "Bed" in t and blo is not None and ("cool" in t or "hotter than" in t
                                                                         or "never set" in t):
            target = round((blo + bhi) / 2 / 5) * 5
            for key in _plate_keys(ctx.trace.header):
                out += [_fix("filament", key, target, t), _fix("filament", key + "_initial_layer", target, t)]
        elif c == "cooling" and "stays off" in t:
            low = max(int((mat.get("fan_pct") or [70, 100])[0]), 70)
            out += [_fix("filament", "fan_min_speed", low, t), _fix("filament", "fan_max_speed", 100, t)]
        elif c == "cooling" and "A lot of cooling" in t:
            out.append(_fix("filament", "fan_max_speed", int((mat.get("fan_pct") or [0, 40])[1]), t))
        elif c == "cooling" and "quick to cool" in t:
            out += [_fix("filament", "slow_down_for_layer_cooling", 1, t), _fix("filament", "slow_down_layer_time", 8, t)]
        elif c == "retraction" and "Long retractions" in t:
            direct = ctx.printer.get("extruder", "direct") == "direct"
            out.append(_fix("filament", "filament_retraction_length", 0.8 if direct else 5, t))
        elif c == "retraction" and "without retracting" in t:
            direct = ctx.printer.get("extruder", "direct") == "direct"
            out.append(_fix("filament", "filament_retraction_length", 0.8 if direct else 5, t))
        elif c == "extrusion" and "filament" in t:
            out.append(_fix("filament", "filament_diameter", ctx.printer.get("filament_diameter", 1.75), t))
        elif c == "adhesion" and "brim" in t:
            out += [_fix("process", "brim_type", "outer_only", t), _fix("process", "brim_width", 6, t)]
        if out:
            f.data["settings"] = out


def merged(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One change per preset key (the first finding's value wins; findings are most severe first)."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for f in findings:
        for s in f.get("settings") or []:
            seen.setdefault((s["preset"], s["key"]), s)
    return list(seen.values())


def as_overrides(changes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for s in changes:
        out.setdefault(s["preset"], {})[s["key"]] = s["value"]
    return out


def user_presets(changes: list[dict[str, Any]], header: dict[str, str], folder: Path) -> dict[str, str]:
    """User presets inheriting from the ones the file was sliced with (File > Import > Import Configs)."""
    folder.mkdir(parents=True, exist_ok=True)
    base = {"filament": (header.get("filament_settings_id") or "").split(";")[0].strip().strip('"'),
            "process": (header.get("print_settings_id") or "").strip().strip('"')}
    written = {}
    for kind in ("filament", "process"):
        mine = [s for s in changes if s["preset"] == kind]
        if not mine:
            continue
        parent = base[kind]
        name = f"{parent} (synthux fixes)" if parent else f"synthux fixes ({kind})"
        data: dict[str, Any] = {"type": kind, "name": name, "from": "User", "instantiation": "true"}
        if parent:
            data["inherits"] = parent
        data["filament_settings_id" if kind == "filament" else "print_settings_id"] = \
            [name] if kind == "filament" else name
        for s in mine:
            data[s["key"]] = [s["value"]] if (kind == "filament" or s["key"] in LIST_PROCESS_KEYS) else s["value"]
        path = folder / f"{kind}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        written[kind] = path.name
    return written
