"""Guided test prints: measure on your machine what ClearLoop must not guess.

Each test takes a small plate you sliced (any tiny part for that printer),
prints it as normal, then runs a test cycle instead of the loop cycle. The
printer pauses with ``M400 U1`` between steps; you watch, then press Resume on
the printer (or stop the print once you have your answer).

* sweep-floor: one centre-lane push at falling heights on an empty plate. The
  lowest height where the pusher doesn't touch the plate is your sweep floor.
* cooldown: the bed-temperature wait alone, then a pause. The bed temperature
  shown at the pause tells you whether ``M190 S`` waited all the way down on
  your firmware.
* door (Stage 2): FarmBoard T1, a pause, then T3.
* bend (Stage 2 with bender): FarmBoard T2 and its wait, then a pause.
"""

from __future__ import annotations

from .gcode.processor import ProcessError, clean_input
from .gcode.routines import block, find_start_anchor, needs_m17, stock_fans_off
from .profiles import HardwareProfile
from .threemf import SlicedFile, layout

TESTS = ("sweep-floor", "cooldown", "door", "bend")
DEFAULT_HEIGHTS = (20.0, 15.0, 10.0, 8.0, 6.0, 5.0, 4.0, 3.0, 2.5, 2.0, 1.5, 1.0)


def _pause(msg: str) -> str:
    return f"; PAUSE: {msg}\nM400\nM400 U1\n"


def build_test(
    sliced: SlicedFile,
    profile: HardwareProfile,
    test: str,
    release_temp: float = 30.0,
    heights: tuple[float, ...] = DEFAULT_HEIGHTS,
) -> tuple[bytes, str]:
    if test not in TESTS:
        raise ProcessError(f"unknown test {test!r}; choose one of {', '.join(TESTS)}")
    if sliced.info.printer_model_id != profile.printer_model_id:
        raise ProcessError("this plate was sliced for a different printer than the profile")
    if test in ("door", "bend") and profile.stage != 2:
        raise ProcessError("this test needs a Stage 2 (FarmBoard) profile")
    if test == "bend" and not profile.has_bender_steps:
        raise ProcessError("this profile has no bend step")
    gcode = clean_input(sliced)
    lay = layout(gcode)
    if not lay.ok:
        raise ProcessError(f"the slice is missing the markers {lay.missing()}")
    end_region = lay.lines[lay.end_gcode_start : lay.exec_end]
    geo = profile.sweep

    body = [f"; ClearLoop calibration test: {test} | profile {profile.id}"]
    if needs_m17(end_region):
        body.append("M17 ; steppers on again: the stock end G-code finished with M18")
    body += ["M400", "M140 S0", "M104 S0"]
    out = "\n".join(body) + "\n"

    if test == "sweep-floor":
        lane = geo.lanes[len(geo.lanes) // 2]
        travel = max(heights) + 10
        out += _pause("remove the printed part so the plate is empty, then press Resume")
        out += f"G1 Z{travel:g} F{geo.z_feed}\nG1 Y{geo.y_rear:g} F{geo.passes[0].feed}\nG1 X{lane:g} F{geo.passes[0].feed}\n"
        for h in sorted(heights, reverse=True):
            out += f"; --- sweep at Z{h:g} ---\nG1 Z{h:g} F{geo.z_feed}\n"
            out += f"G1 Y{geo.y_front:g} F{geo.passes[0].feed}\nG1 Y{geo.y_rear:g} F{geo.passes[0].feed}\n"
            out += f"G1 Z{travel:g} F{geo.z_feed}\n"
            out += _pause(
                f"that pass was at Z{h:g}. If the pusher touched the plate, stop the print: your floor is the previous height"
            )
        out += "\n".join(geo.park) + "\n"
    elif test == "cooldown":
        out += f"M190 S{release_temp:g}\n" * max(1, profile.cooldown_repeat)
        out += "M140 S0\n"
        out += _pause(
            f"write down the bed temperature on the printer screen now (target was {release_temp:g} C)"
        )
    elif test == "door":
        out += profile.door_open
        out += _pause("the door should be open; press Resume to close it")
        out += profile.door_close
    elif test == "bend":
        out += profile.bend
        out += _pause("the bend cycle should be finished; press Resume to end the test")
        if profile.safety:
            out += profile.safety
        out += profile.door_close
    off = profile.fans_off or stock_fans_off(end_region)
    if off:
        out += "\n".join(off) + "\n"

    lines = list(lay.lines[: lay.exec_end + 1]) + lay.lines[lay.exec_end + 1 :]
    lines.insert(lay.exec_end, block("TEST", out))
    if profile.start_block:
        i = find_start_anchor(lay.lines, lay.exec_start, lay.start_gcode_end, profile.start_anchor or "^$")
        if i < 0:
            raise ProcessError("can't find where the FarmBoard start step belongs in this slice")
        lines.insert(i + 1, block("START", profile.start_block))
    text = "".join(lines)
    base = (sliced.name or "plate.gcode.3mf").rsplit("/", 1)[-1]
    return sliced.to_bytes(text), f"FL_S{profile.stage}_CALIBRATION-{test}_{base}"
