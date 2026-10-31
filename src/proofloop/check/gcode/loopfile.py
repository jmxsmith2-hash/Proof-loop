"""Loop files (FarmLoop auto-eject): what the file does, and the loop-file checks on its ejection cycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from proofloop.check.gcode.context import GcodeContext, _at_moves
from proofloop.core.logging import get_logger

log = get_logger(__name__)

LOOP_LEVELS = {"danger": 4, "warning": 3, "info": 1}


def is_loop_file(trace: Any, loops: int) -> bool:
    name = Path(trace.source or trace.path or "").name
    return (loops > 1 or trace.marks.get("proofloop_blocks", 0) > 0 or trace.marks.get("farmloop_sections", 0) > 0
            or name.startswith(("FL_S1_", "FL_S2_")))


def check_loop_file(ctx: GcodeContext) -> None:
    """Loop files (FarmLoop auto-eject): what the file does, plus the loop-file checks on its ejection cycle.

    The print checks above already look at each loop's print only; the ejection cycle after it
    (cooldown, door, bend, sweep, plate-check pause) gets the loop-file checks (``loopfiles.lint``),
    which need the whole .gcode.3mf. A plain .gcode loop file is checked for heated waits only.
    """
    from proofloop.loopfiles import threemf
    from proofloop.loopfiles.lint import lint
    from proofloop.loopfiles.threemf import PlateFileError

    t = ctx.trace
    loops = ctx.metrics.get("loops", 1)
    if not is_loop_file(t, loops):
        return
    plates = {(a, b) for a, b in zip(t.header_all.get("model label id", []) or [""] * loops,
                                    t.header_all.get("total layer number", []) or [""] * loops, strict=False)}
    made_by = ("ProofLoop" if t.marks.get("proofloop_blocks") else
               "the FarmLoop app" if t.marks.get("farmloop_sections") else "another loop tool")
    info: dict[str, Any] = {"loops": loops, "plates": max(1, len(plates)), "made_by": made_by,
                            "plate_check_pauses": t.marks.get("loop_pauses", 0), "loop_checks": None}
    ctx.metrics["loop_file"] = info
    path = t.path or ""
    if not path.lower().endswith(".3mf"):
        heated_waits_before_print(ctx)
        return
    try:
        sliced = threemf.load(path, t.plate)
    except (PlateFileError, OSError) as exc:
        ctx.add("loop_file", 1, "The loop-file checks couldn't read this file", str(exc))
        return
    try:
        found = lint(sliced)
    except Exception as exc:  # noqa: BLE001 - broad on purpose: a bug in the linter must not hide the print checks
        log.warning("loop-file lint failed on %s: %s", path, exc)
        ctx.add("loop_file", 1, "The loop-file checks couldn't read this file", str(exc))
        return
    info["loop_checks"] = [{"level": f.level, "title": f.title, "detail": f.detail} for f in found]
    for f in found:
        if f.level in LOOP_LEVELS:
            ctx.add("loop_file", LOOP_LEVELS[f.level], f"Loop file: {f.title}", f.detail,
                    "Make the loop file again with ProofLoop (Loop files tab, or proofloop loop), which fixes "
                    "the known problems.")


def heated_waits_before_print(ctx: GcodeContext) -> None:
    """A long wait in the start code with a heater on (FarmLoop app 2.72's 'natural cooldown' bug)."""
    t = ctx.trace
    if t.phase is None or not t.dwell_events:
        return
    n = len(t)
    noz = _at_moves([(i, v) for i, v, _ in t.nozzle_events], n + 1, default=0.0)
    bed = _at_moves(t.bed_events, n + 1, default=0.0)
    phase = np.append(t.phase, t.phase[-1] if n else 1)
    loop = np.append(t.loop, t.loop[-1] if n else 0)
    waits: dict[int, float] = {}
    for i, sec in t.dwell_events:
        i = min(i, n)
        if phase[i] == 0 and (noz[i] > 5 or bed[i] > 5):    # 1-5 °C are FarmBoard signals, not heat
            waits[int(loop[i])] = waits.get(int(loop[i]), 0.0) + sec
    worst = max(waits.values(), default=0.0)
    if worst >= 600:
        ctx.add("loop_file", 4, f"{worst / 60:.0f} min wait before printing, heaters on",
                "The start code waits with the nozzle or bed heating before the print begins: the printer looks "
                "frozen and stays hot (the FarmLoop app 2.72 'natural cooldown' placement bug).",
                "Don't run this file. Make it again with the cooldown after the print (ProofLoop does this).")
