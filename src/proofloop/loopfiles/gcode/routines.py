"""Assemble the G-code ProofLoop adds to a plate.

Placement follows what is known to run on Bambu printers:

* The end-of-print cycle goes **immediately before** ``; EXECUTABLE_BLOCK_END``,
  after the printer's own end G-code. Bambu firmware does not run G-code placed
  after that marker (Bambuddy's injector, verified on a P1S; the P2S playbook's
  live runs use the same placement).
* Stock Bambu end G-code finishes with ``M18`` (steppers off), so the cycle
  starts with ``M17`` before moving anything.
* The FarmBoard start step goes where the FarmLoop app puts it, right after the
  start G-code's date line.
* The cooldown waits with ``M190 S<t>``, as the FarmLoop app does. ``M190 R`` is
  never used: on a P2S it raised HMS 0300-0100-0003-0008 and drove the bed
  target up instead of down.
* Pauses use ``M400 U1``, Bambu's own pause command (``machine_pause_gcode`` in
  Bambu Studio's printer profiles).

Every block is wrapped in ``; >>> PROOFLOOP <name>`` / ``; <<< PROOFLOOP <name>``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from proofloop.loopfiles.gcode.sweep import SweepPlan, SweepSettings, sweep_gcode
from proofloop.loopfiles.profiles import HardwareProfile

BLOCK_RE = re.compile(r"; >>> PROOFLOOP (\w+)\n.*?; <<< PROOFLOOP \1\n", re.S)
FAN_OFF_RE = re.compile(r"^\s*M106\b(?:\s+P\d+)?\s+S0\b")
STEPPER_RE = re.compile(r"^\s*(M17|M18|M84)\b")
# Bambu Studio: ";===== 2026/05/18 =====". OrcaSlicer: ";===== Based on official 2026/04/21 start gcode =====".
DATE_ANCHOR = r"^;=+ (?:Based on official )?\d{4}/\d{2}/\d{2}(?: start gcode)? =+$"
_BAMBU_DATE_ANCHOR = r"^;=+ \d{4}/\d{2}/\d{2} =+$"  # in profiles imported before OrcaSlicer was supported
DATE_LINE = re.compile(DATE_ANCHOR)

# P2S fan indexes, from the printer's stock end G-code template in Bambu Studio:
# P2 = "remote part cooling fan" (aux), P3 = "chamber cooling fan" (exhaust).
# ProofLoop's own signal, like FarmBoard's M104 S1..S5: tells the Monitor the next pause is the
# first-sweep safety pause (wait for you), not the loop-end pause (check the plate and resume).
SAFETY_PAUSE_SIGNAL = 6
P2S_EXHAUST_FAN = "M106 P3 S255 ; exhaust (chamber) fan: vents the chamber while cooling"


@dataclass
class LoopSettings:
    loops: int = 1
    release_temp: float = 30.0  # FarmLoop's default cooldown target
    natural_cooldown_min: int = 0  # 0 = off; FarmLoop allows 1-120 min
    bender_fitted: bool = True
    cooldown_fan: str = "profile"  # "profile" (the kit's own), "exhaust" (P2S only) or "off"
    wait_for_monitor: bool = False  # pause after each loop until ProofLoop's printer monitor confirms an empty plate
    pause_first_sweep: bool = False  # pause before loop 1's sweep so you can check the pusher height
    sweep: SweepSettings = field(default_factory=SweepSettings)


def block(name: str, body: str) -> str:
    if body and not body.endswith("\n"):
        body += "\n"
    return f"; >>> PROOFLOOP {name}\n{body}; <<< PROOFLOOP {name}\n"


def strip_blocks(gcode: str) -> str:
    return BLOCK_RE.sub("", gcode)


def needs_m17(end_region: list[str]) -> bool:
    """True when the stock end G-code leaves the steppers switched off."""
    last = None
    for ln in end_region:
        m = STEPPER_RE.match(ln.split(";", 1)[0])
        if m:
            last = m.group(1)
    return last in ("M18", "M84")


def stock_fans_off(end_region: list[str]) -> list[str]:
    seen, out = set(), []
    for ln in end_region:
        code = ln.split(";", 1)[0].strip()
        if FAN_OFF_RE.match(code) and code not in seen:
            seen.add(code)
            out.append(ln.strip())
    return out


def find_start_anchor(lines: list[str], lo: int, hi: int, anchor: str) -> int:
    pattern = re.compile(DATE_ANCHOR if anchor == _BAMBU_DATE_ANCHOR else anchor)
    for i in range(lo, hi):
        if pattern.match(lines[i].rstrip("\r\n").strip()):
            return i
    return -1


def generalize_anchor(anchor_line: str) -> str:
    """Bambu stamps a date on its start template; anchor on 'the date line', not one specific date."""
    if DATE_LINE.match(anchor_line.strip()):
        return DATE_ANCHOR
    return "^" + re.escape(anchor_line.strip()) + "$"


def end_block(
    profile: HardwareProfile,
    settings: LoopSettings,
    plan: SweepPlan,
    end_region: list[str],
    is_p2s: bool,
    last_loop: bool,
    loop_no: int,
) -> str:
    g: list[str] = [
        f"; ProofLoop end-of-print cycle, loop {loop_no}/{settings.loops} | profile {profile.id} | "
        f"release {settings.release_temp:g} C | sweep floor Z{settings.sweep.floor_z:g}",
    ]
    if needs_m17(end_region):
        g.append("M17 ; steppers on again: the stock end G-code finished with M18")
    g += ["M400", "M140 S0 ; bed heater off", "M104 S0 ; nozzle heater off"]

    if settings.natural_cooldown_min > 0:
        g.append(
            f"; warp-safe natural cooldown, {settings.natural_cooldown_min} min: heaters off, fans off, door shut"
        )
        g += stock_fans_off(end_region) or ["M106 S0"]
        g += ["G4 S30"] * (settings.natural_cooldown_min * 2)

    fan_lines: list[str] = []
    if settings.cooldown_fan == "profile":
        fan_lines = list(profile.cooldown_fan)
    elif settings.cooldown_fan == "exhaust":
        if not is_p2s:
            raise ValueError("the exhaust-fan option is only known for the P2S")
        fan_lines = [P2S_EXHAUST_FAN]
    g += fan_lines

    out = "\n".join(g) + "\n"
    if profile.door_open:
        out += profile.door_open
    out += f"; cooldown: wait until the bed is at {settings.release_temp:g} C\n"
    out += f"M190 S{settings.release_temp:g}\n" * max(1, profile.cooldown_repeat)
    out += "M140 S0\nM104 S0\n"

    use_bender = settings.bender_fitted and profile.has_bender_steps
    if use_bender:
        out += profile.bend
    if settings.pause_first_sweep and loop_no == 1:
        out += (
            "; SAFETY PAUSE before the first sweep on this setup: the parts are cool and the pusher is next.\n"
            "; Check the push heights against the parts (and that nothing is in the way), then press Resume.\n"
            f"M104 S{SAFETY_PAUSE_SIGNAL} ; ProofLoop signal for the Monitor (heaters stay off)\n"
            "M400\nM400 U1\nM104 S0\n"
        )
    out += sweep_gcode(plan, profile.sweep)
    if use_bender and profile.safety:
        out += profile.safety
    if profile.door_close:
        out += profile.door_close
    off = profile.fans_off or stock_fans_off(end_region)
    if off:
        out += "\n".join(off) + "\n"
    if settings.wait_for_monitor and not last_loop:
        out += (
            "; wait here until ProofLoop's printer monitor has checked the plate with the camera and resumes the print.\n"
            "; If the monitor is not running, press Resume on the printer once the plate is empty.\n"
            "M400 U1\n"
        )
    out += "M400\n"
    return block("END", out)
