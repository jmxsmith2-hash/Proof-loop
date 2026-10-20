"""G-code blocks injected around a sliced plate.

Every block is wrapped in ``; >>> CLEARLOOP <name>`` / ``; <<< CLEARLOOP <name>``
so a file can be processed again without stacking routines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .. import protocol
from ..profiles import MaterialProfile, PrinterProfile
from .sweep import SweepPlan, SweepSettings

BLOCK_RE = re.compile(r"; >>> CLEARLOOP (\w+)\n.*?; <<< CLEARLOOP \1\n", re.S)

EjectMode = Literal["board", "gcode", "custom"]
CooldownMode = Literal["temperature", "time"]


@dataclass
class LoopSettings:
    eject_mode: EjectMode = "board"
    cooldown_mode: CooldownMode = "temperature"
    release_temp: float | None = None  # None: take it from the material
    cooldown_time_s: int = 600  # used when cooldown_mode == "time"
    natural_cooldown_min: int | None = None  # None: automatic from material/chamber
    min_cooldown_s: int = 30  # always wait at least this long after the fans come on
    door_opener: bool = False
    bend_window_s: int = 25  # printer dwells this long while the ejector bends
    retract_wait_s: int = 4  # dwell after JOB_START so the ejector retracts before homing
    custom_eject_gcode: str = ""  # eject_mode == "custom": your hardware's snippet
    marker_base: int = protocol.DEFAULT_BASE
    marker_hold_s: int = protocol.DEFAULT_HOLD_S
    sweep: SweepSettings = field(default_factory=SweepSettings)


def block(name: str, body: str) -> str:
    if not body.endswith("\n"):
        body += "\n"
    return f"; >>> CLEARLOOP {name}\n{body}; <<< CLEARLOOP {name}\n"


def strip_blocks(gcode: str) -> str:
    return BLOCK_RE.sub("", gcode)


def _dwell(seconds: int) -> str:
    out = []
    while seconds > 0:
        chunk = min(60, seconds)
        out.append(f"G4 S{chunk}")
        seconds -= chunk
    return "\n".join(out) + ("\n" if out else "")


def _marker(m: protocol.Marker, s: LoopSettings) -> str:
    return protocol.encode(m, s.marker_base, s.marker_hold_s)


def start_block(s: LoopSettings) -> str:
    body = "; ejector retracts and the door closes before homing or bed levelling\n"
    body += _marker(protocol.Marker.JOB_START, s)
    if s.eject_mode == "board" and s.retract_wait_s > 0:
        body += _dwell(s.retract_wait_s)
    return block("START", body)


def natural_cooldown_minutes(
    s: LoopSettings, material: MaterialProfile, printer: PrinterProfile, chamber_temp: float
) -> int:
    if s.natural_cooldown_min is not None:
        return s.natural_cooldown_min
    if not printer.enclosed:
        return 0
    minutes = material.natural_cooldown_min
    if chamber_temp > 0:
        # Heated-chamber jobs: let the chamber come down with the door shut.
        minutes = max(minutes, 20)
    return minutes


def loop_block(
    plan: SweepPlan,
    printer: PrinterProfile,
    material: MaterialProfile,
    s: LoopSettings,
    chamber_temp: float = 0.0,
) -> str:
    release = s.release_temp if s.release_temp is not None else material.release_temp
    natural = natural_cooldown_minutes(s, material, printer, chamber_temp)
    g: list[str] = [
        f"; ClearLoop protocol v{protocol.PROTOCOL_VERSION} | material {material.name} | release {release:g} C"
        f" | natural cooldown {natural} min | {len(plan.passes)} sweep passes",
        "M400",
        "M104 S0 ; nozzle heater off",
        "M140 S0 ; bed heater off",
    ]
    if natural > 0:
        g += [
            "; warp-safe natural cooldown: door shut, fans off",
            "M106 P1 S0",
            "M106 P2 S0",
            "M106 P3 S0",
            _dwell(natural * 60).rstrip("\n"),
        ]
    out = "\n".join(g) + "\n"
    if printer.enclosed and s.door_opener:
        out += _marker(protocol.Marker.DOOR_OPEN, s)
    out += "M106 P1 S255 ; part fan\nM106 P2 S255 ; aux fan\n"
    if printer.enclosed:
        out += "M106 P3 S255 ; chamber fan\n"
    out += _dwell(s.min_cooldown_s)
    if s.cooldown_mode == "temperature":
        out += f"M190 R{release:g} ; wait for the bed to cool to the release temperature\n"
        out += "M140 S0 ; M190 R leaves that temperature as the target; switch the heater off again\n"
    else:
        out += _dwell(s.cooldown_time_s)
    out += "M106 P1 S0\nM106 P2 S0\n"
    if printer.enclosed:
        out += "M106 P3 S0\n"
    out += "G90\n"

    if s.eject_mode == "board":
        out += f"G1 Z{printer.bend_z:g} F{printer.z_feed} ; present the plate to the bender\n"
        out += _marker(protocol.Marker.EJECT, s)
        out += _dwell(s.bend_window_s)
    elif s.eject_mode == "custom":
        out += (
            "; ---- user eject snippet ----\n" + s.custom_eject_gcode.strip() + "\n; ---- end snippet ----\n"
        )
    else:
        out += _marker(protocol.Marker.EJECT, s)

    out += sweep_gcode(plan, printer)
    out += _marker(protocol.Marker.SWEEP_DONE, s)
    park_z = min(printer.max_z, max((p.travel_z for p in plan.passes), default=20.0))
    out += f"G1 Z{park_z:g} F{printer.z_feed}\nG1 X{printer.bed_x / 2:g} Y{printer.bed_y:g} F{printer.travel_feed}\nM400\n"
    return block("LOOP", out)


def sweep_gcode(plan: SweepPlan, printer: PrinterProfile) -> str:
    lines = []
    for i, p in enumerate(plan.passes):
        tag = f"object {p.target}" if p.target is not None else "wipe"
        lines += [
            f"; sweep {i + 1}/{len(plan.passes)} ({tag}) push at Z{p.push_z:.2f}",
            f"G1 Z{p.travel_z:.2f} F{printer.z_feed}",
            f"G1 X{p.x:.2f} Y{p.y_start:.2f} F{printer.travel_feed}",
            f"G1 Z{p.push_z:.2f} F{printer.z_feed}",
            f"G1 Y{p.y_end:.2f} F{printer.push_feed}",
            f"G1 Z{p.travel_z:.2f} F{printer.z_feed}",
        ]
    return "\n".join(lines) + ("\n" if lines else "")


def loop_end_block(s: LoopSettings) -> str:
    return block("LOOPEND", _marker(protocol.Marker.LOOP_END, s))


def recovery_gcode(printer: PrinterProfile, s: LoopSettings, plan: SweepPlan) -> str:
    """A stand-alone 'clear the plate' job used when verification finds leftovers."""
    head = "; ClearLoop recovery sweep\nG90\nM83\nM104 S0\nM140 S0\n"
    body = head
    if s.eject_mode == "board":
        body += f"G1 Z{printer.bend_z:g} F{printer.z_feed}\n"
        body += _marker(protocol.Marker.EJECT, s) + _dwell(s.bend_window_s)
    elif s.eject_mode == "custom":
        body += s.custom_eject_gcode.strip() + "\n"
    body += sweep_gcode(plan, printer)
    body += _marker(protocol.Marker.SWEEP_DONE, s)
    return block("RECOVERY", body)


_BED_TEMP_RE = re.compile(r"^(M1(?:40|90))(\s+[^;\n]*?)S(\d+(?:\.\d+)?)", re.M)


def override_bed_temp(gcode: str, temp: float) -> str:
    """Set every non-zero bed target to exactly ``temp`` (no hidden offsets)."""

    def repl(m: re.Match) -> str:
        if float(m.group(3)) == 0:
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}S{temp:g}"

    return _BED_TEMP_RE.sub(repl, gcode)
