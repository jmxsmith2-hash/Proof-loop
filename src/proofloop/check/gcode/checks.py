"""Checks on sliced G-code: will this file print well on this printer, with this material?

Each check measures the file itself (every move, temperature and fan command) against the printer
profile and the material's usual ranges from config/printers.yaml:

* bounds        - extrusion outside the build plate or above the build height, or below the bed
* printer       - sliced for another printer or nozzle than the one you chose
* first_layer   - first-layer height, speed and bed temperature
* layer_height  - layers too thick for the nozzle (weak bonding) or extremely thin
* temperature   - nozzle and bed against the material and the printer's limits; extruding cold
* flow          - melt rate (mm³/s) the moves ask for against the hot end and the material
* extrusion     - line widths that imply the wrong filament diameter or flow
* retraction    - retraction length for the extruder type; long travels without retraction
* cooling       - part fan against what the material wants; layers too short to cool
* adhesion      - warping materials on open printers, tall narrow parts without a brim
* end           - heaters left on at the end
* consistency   - the file against its own header (edited after slicing? another plate?)

Speeds are the ones commanded; the printer may go slower on short moves and corners,
so flow figures are upper bounds for short moves and close to reality on long ones.

The checks live in one module per family (plate, heat, travel, header, loopfile, history); this
module runs them in order and re-exports their names.
"""

from __future__ import annotations

from typing import Any

from proofloop.check.gcode.context import (
    COLD_EXTRUSION_C,
    DEFAULT_ACCEL,
    GcodeContext,
    _at_moves,
    _where,
    prepare,
)
from proofloop.check.gcode.header import (
    check_consistency,
    loop_times,
    parse_duration,
    slicer_notes,
)
from proofloop.check.gcode.heat import check_cooling, check_extrusion, check_flow, check_temperatures
from proofloop.check.gcode.history import check_history
from proofloop.check.gcode.loopfile import (
    LOOP_LEVELS,
    check_loop_file,
    heated_waits_before_print,
    is_loop_file,
)
from proofloop.check.gcode.plate import check_adhesion, check_bounds, check_end, check_layers, check_printer
from proofloop.check.gcode.travel import check_retraction, travels_over_air, unretracted_travels

__all__ = [
    "COLD_EXTRUSION_C", "DEFAULT_ACCEL", "LOOP_LEVELS", "GcodeContext", "_at_moves", "_where", "check_adhesion",
    "check_bounds", "check_consistency", "check_cooling", "check_end", "check_extrusion", "check_flow",
    "check_history", "check_layers", "check_loop_file", "check_printer", "check_retraction", "check_temperatures",
    "heated_waits_before_print", "is_loop_file", "loop_times", "parse_duration", "prepare", "run_gcode_checks",
    "slicer_notes", "travels_over_air", "unretracted_travels",
]


def run_gcode_checks(ctx: GcodeContext, notes: dict[str, Any]) -> GcodeContext:
    prepare(ctx)
    check_bounds(ctx)
    check_printer(ctx, notes.get("printer"), notes.get("nozzle"))
    check_layers(ctx)
    check_temperatures(ctx)
    check_flow(ctx)
    check_extrusion(ctx, notes.get("filament_diameter"))
    check_retraction(ctx)
    check_cooling(ctx)
    check_adhesion(ctx)
    check_end(ctx)
    check_consistency(ctx, notes.get("filament_mm"), notes.get("filament_g"),
                      float(ctx.material.get("density_g_cm3", 1.24)))
    check_loop_file(ctx)
    check_history(ctx)
    replay = float(ctx.time[ctx.model].sum())                    # the prints themselves
    ctx.metrics["replay_time_s"] = round(replay)
    ctx.metrics["slicer_time_s"] = round(notes["time_s"]) if notes.get("time_s") else None
    loops = ctx.metrics.get("loops", 1)
    if loops > 1:
        per_loop = loop_times(ctx.trace.header_all)
        if per_loop:
            ctx.metrics["slicer_time_s"] = round(sum(per_loop) if len(per_loop) == loops else per_loop[0] * loops)
        ctx.metrics["dwell_s"] = round(ctx.trace.dwell_s)
    ctx.metrics["travel_m"] = round(float(ctx.xy[ctx.travel].sum()) / 1000, 1)
    ctx.metrics["tool_changes"] = ctx.trace.tool_changes
    feats = {}
    for i, name in enumerate(ctx.trace.features):
        s = ctx.extrude & (ctx.trace.feature == i) & ctx.model
        if s.any():
            feats[name] = round(float(ctx.time[s].sum()) / 60, 1)
    ctx.metrics["minutes_by_feature"] = dict(sorted(feats.items(), key=lambda kv: -kv[1]))
    if ctx.trace.tool_changes:
        ctx.add("consistency", 1, f"{ctx.trace.tool_changes} filament changes",
                "Each change purges filament (often 1-3 g each with an AMS) and adds about a minute.",
                "Group colours by layer, or print a single colour, to save filament.")
    ctx.findings.sort(key=lambda f: -f.severity)
    from proofloop.check.gcode.settings_fixes import fixes_for

    fixes_for(ctx)
    return ctx
