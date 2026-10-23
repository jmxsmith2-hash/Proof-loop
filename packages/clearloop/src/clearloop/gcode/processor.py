"""Turn a plain sliced plate into a FarmLoop-hardware loop file."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .. import printers
from ..profiles import HardwareProfile
from ..threemf import SlicedFile, layout
from .objects import Part, check_heights, read_parts
from .routines import (
    BLOCK_RE,
    DATE_LINE,
    LoopSettings,
    block,
    end_block,
    find_start_anchor,
    strip_blocks,
)
from .sweep import SweepPlan, plan_sweep, sweep_path, sweep_seconds

# Materials FarmLoop names as needing natural cooldown (warp when cooled fast), and rigid-only support.
WARP_PRONE = ("ASA", "ABS", "PC")
NOT_SUPPORTED = ("TPU", "TPE", "FLEX")
DWELL_RE = re.compile(r"^\s*(?:G4\s+([PS])(\d+(?:\.\d+)?)|M400\s+S(\d+(?:\.\d+)?))")
FARMLOOP_START = re.compile(r"^M104\s+S[1-5]\s*;\s*FL_S", re.M)
FARMLOOP_SECTION = re.compile(r"^; @fl:section", re.M)
LOOP_SEPARATOR = "\n; === CLEARLOOP LOOP "


class ProcessError(ValueError):
    """The file can't be turned into a safe loop file. The message says what to do."""


@dataclass
class Analysis:
    sliced: SlicedFile
    printer: str
    parts: list[Part]
    plan: SweepPlan
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    hardware_wait_s: float = 0.0
    sweep_s: float = 0.0
    steps: list[dict] = field(default_factory=list)
    path: list[dict] = field(default_factory=list)  # the sweep's toolhead moves, for the animation

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        info = self.sliced.info
        return {
            "file": self.sliced.name,
            "plate": self.sliced.plate,
            "printer": self.printer,
            "printer_model_id": info.printer_model_id,
            "slicer": info.slicer,
            "bed": [info.bed_x, info.bed_y],
            "printable_height": info.printable_height,
            "bed_type": info.bed_type,
            "filaments": info.filaments,
            "filament_slots": [list(x) for x in info.filament_slots],
            "presets": info.presets,
            "print_seconds": info.print_seconds,
            "parts": [p.as_dict() for p in self.parts],
            "plan": self.plan.as_dict(),
            "warnings": self.warnings,
            "problems": self.problems,
            "notes": self.notes,
            "hardware_wait_s": round(self.hardware_wait_s),
            "sweep_s": round(self.sweep_s),
            "steps": self.steps,
            "path": self.path,
            "ok": self.ok,
        }


@dataclass
class Result:
    data: bytes
    gcode: str
    filename: str
    analysis: Analysis


def dwell_seconds(text: str) -> float:
    total = 0.0
    for ln in text.splitlines():
        m = DWELL_RE.match(ln.split(";", 1)[0])
        if not m:
            continue
        if m.group(1) == "P":
            total += float(m.group(2)) / 1000
        elif m.group(1) == "S":
            total += float(m.group(2))
        elif m.group(3):
            total += float(m.group(3))
    return total


def _config_float(gcode: str, key: str) -> float:
    m = re.search(rf"^; {key} = ([\d.]+)", gcode[:400_000], re.M)
    return float(m.group(1)) if m else 0.0


def clean_input(sliced: SlicedFile) -> str:
    """The plain slice: refuse FarmLoop output, and undo ClearLoop's own processing."""
    gcode = sliced.gcode
    if BLOCK_RE.search(gcode):
        # One of our own files: keep only the first loop, then remove ClearLoop's blocks
        # (they contain FarmLoop's gestures verbatim, so this must happen before the check below).
        gcode = strip_blocks(gcode.split(LOOP_SEPARATOR, 1)[0])
    cut = gcode.find("; MACHINE_START_GCODE_END")
    head = gcode[: cut if cut >= 0 else 400_000]
    if FARMLOOP_START.search(head) or FARMLOOP_SECTION.search(gcode) or "FL NATURAL COOLDOWN" in head:
        raise ProcessError(
            "this file was already processed by the FarmLoop app. Use the plain slice from your slicer instead "
            "(ClearLoop can still check this file for problems in the 'Check a file' tab)."
        )
    return gcode


def analyze(sliced: SlicedFile, profile: HardwareProfile, settings: LoopSettings) -> Analysis:
    info = sliced.info
    if not info.printer_model_id:
        raise ProcessError(
            "the file doesn't say which printer it was sliced for (no printer_model_id); "
            "slice it in Bambu Studio or OrcaSlicer with a Bambu Lab printer profile"
        )
    printer = printers.model_name(info.printer_model_id)
    if info.printer_model_id != profile.printer_model_id:
        raise ProcessError(
            f"this plate was sliced for the {printer}, but '{profile.name}' is a profile for the "
            f"{printers.model_name(profile.printer_model_id)}. Pick a profile for the {printer} or import one."
        )
    gcode = clean_input(sliced)
    lay = layout(gcode)
    if not lay.ok:
        raise ProcessError(
            f"the slice is missing the markers {lay.missing()}; re-slice with a current Bambu Studio"
        )
    if not sliced.md5_ok:
        raise ProcessError(
            "the plate's checksum doesn't match its G-code: the file was edited or damaged. Re-export it."
        )

    parts = read_parts(gcode, info.object_names, info.object_heights)
    if not parts:
        raise ProcessError(
            "no labelled objects in the G-code, so ClearLoop can't see where the parts are. In Bambu Studio this "
            "is on by default; in OrcaSlicer turn on 'Label objects' (Others > G-code output) and re-slice."
        )
    a = Analysis(
        sliced, printer, parts, plan_sweep(parts, profile.sweep, info.printable_height, settings.sweep)
    )
    a.problems += check_heights(parts)
    a.problems += a.plan.problems
    a.warnings += a.plan.warnings

    upper = [f.upper() for f in info.filaments]
    if any(any(u.startswith(b) for b in NOT_SUPPORTED) for u in upper):
        a.problems.append(
            f"{', '.join(info.filaments)}: FarmLoop ejection is for rigid materials only; flexible parts can't be "
            "pushed off reliably."
        )
    warp = [f for f in info.filaments if f.upper().startswith(WARP_PRONE)]
    if warp and settings.natural_cooldown_min == 0 and profile.stage == 2:
        a.warnings.append(
            f"{', '.join(warp)} warps if the door opens and fans hit the parts while they're hot. Consider a "
            "natural cooldown (door shut, fans off) before the door opens. Choose the minutes for your parts."
        )
    if profile.stage == 1:
        a.notes.append(
            "Stage 1 has no door opener: leave the printer door open (or removed) so parts can leave."
        )
        if settings.natural_cooldown_min:
            a.warnings.append(
                "natural cooldown keeps fans off, but with the door open (Stage 1) the chamber isn't closed"
            )
    if settings.cooldown_fan == "exhaust" and info.printer_model_id != printers.P2S:
        a.problems.append("the exhaust-fan cooldown option is only defined for the P2S")
    if not settings.bender_fitted and profile.stage == 2:
        a.notes.append(
            "Bender removed: skipping T2 (bend) and the safety check. The door still closes on T3, as seen on a P2S."
        )

    # Start step anchor (FarmBoard reset/close-door goes where the FarmLoop app puts it).
    if profile.start_block:
        i = find_start_anchor(lay.lines, lay.exec_start, lay.start_gcode_end, profile.start_anchor or "^$")
        if i < 0:
            a.problems.append(
                "can't find where the FarmBoard start step belongs: this slice's start G-code doesn't contain "
                "the line the profile anchors to. Your slicer's printer template may differ from the one the "
                "profile was learned from; import a profile from a FarmLoop file made with your slicer."
            )
        else:
            learned = profile.source.get("start_template_line", "")
            here = lay.lines[i].strip()
            if learned and DATE_LINE.match(learned) and learned != here:
                a.warnings.append(
                    f"the profile was learned from start template '{learned}', this slice uses '{here}'. "
                    "The FarmBoard start step goes in the same place; watch the first loop."
                )

    a.path = sweep_path(a.plan, profile.sweep)
    use_bender = settings.bender_fitted and profile.has_bender_steps
    a.hardware_wait_s = dwell_seconds(profile.door_open + profile.door_close + profile.start_block)
    if use_bender:
        a.hardware_wait_s += dwell_seconds(profile.bend + profile.safety)
    a.sweep_s = sweep_seconds(
        a.plan,
        profile.sweep,
        _config_float(gcode, "machine_max_speed_x"),
        _config_float(gcode, "machine_max_speed_z"),
    )

    # The loop, step by step. Durations are only given where the file fixes them; the cooldown
    # depends on your room and plate, so it has none.
    def step(key, name, seconds, detail):
        a.steps.append(
            {
                "key": key,
                "name": name,
                "seconds": None if seconds is None else round(seconds),
                "detail": detail,
            }
        )

    if profile.start_block:
        step(
            "start",
            "FarmBoard reset",
            dwell_seconds(profile.start_block),
            "bender home, door closed, then the print starts",
        )
    step("print", "Print", info.print_seconds or None, "slicer estimate")
    if settings.natural_cooldown_min:
        step(
            "natural",
            "Natural cooldown",
            settings.natural_cooldown_min * 60,
            "heaters off, fans off, door shut",
        )
    if profile.door_open:
        step("door_open", "Door opens", dwell_seconds(profile.door_open), "FarmBoard T1 (door + fan)")
    step(
        "cool",
        f"Cool to {settings.release_temp:g} °C",
        None,
        "bed-temperature wait; time depends on room and plate",
    )
    if use_bender:
        step(
            "bend",
            "Bend plate",
            dwell_seconds(profile.bend),
            "FarmBoard T2 and its wait, as FarmLoop times it",
        )
    passes = sum(len(ln.tiers) for p in a.plan.passes for ln in p.lanes)
    step("sweep", "Sweep", a.sweep_s, f"{passes} pushes, each part at its own height")
    if use_bender and profile.safety:
        step("safety", "Safety check", dwell_seconds(profile.safety), "FarmBoard safety sequence")
    if profile.door_close:
        step("door_close", "Door closes", dwell_seconds(profile.door_close), "FarmBoard T3")
    if settings.wait_for_monitor and settings.loops > 1:
        step("check", "Plate check", None, "printer waits until ClearLoop Monitor sees an empty plate")
    return a


def output_name(name: str, profile: HardwareProfile) -> str:
    base = name.rsplit("/", 1)[-1] or "plate.gcode.3mf"
    base = re.sub(r"^(FL_S[12]_)+", "", base)
    # The FarmLoop docs say FarmBoard kits key off this prefix; keep it so the hardware behaves the same.
    return f"FL_S{profile.stage}_{base}"


def _loop_text(
    gcode: str,
    profile: HardwareProfile,
    settings: LoopSettings,
    plan: SweepPlan,
    is_p2s: bool,
    loop_no: int,
    last: bool,
) -> str:
    """One complete plate (header, config, executable block) with ClearLoop's blocks added."""
    lay = layout(gcode)
    lines = lay.lines
    end_region = lines[lay.end_gcode_start : lay.exec_end]
    out = list(lines)
    out.insert(lay.exec_end, end_block(profile, settings, plan, end_region, is_p2s, last, loop_no))
    if profile.start_block:
        i = find_start_anchor(lines, lay.exec_start, lay.start_gcode_end, profile.start_anchor or "^$")
        out.insert(i + 1, block("START", profile.start_block))
    return "".join(out)


def _join_loops(bodies: list[str]) -> str:
    # Same multi-loop layout as the FarmLoop app: whole plates, one after the other.
    n = len(bodies)
    return "".join((f"{LOOP_SEPARATOR}{k + 1} OF {n} ===\n" if k else "") + b for k, b in enumerate(bodies))


def process(sliced: SlicedFile, profile: HardwareProfile, settings: LoopSettings) -> Result:
    if settings.loops < 1:
        raise ProcessError("loops must be at least 1")
    a = analyze(sliced, profile, settings)
    if a.problems:
        raise ProcessError("; ".join(a.problems))
    gcode = clean_input(sliced)
    is_p2s = sliced.info.printer_model_id == printers.P2S
    bodies = [
        _loop_text(gcode, profile, settings, a.plan, is_p2s, k, k == settings.loops)
        for k in range(1, settings.loops + 1)
    ]
    text = _join_loops(bodies)
    return Result(sliced.to_bytes(text), text, output_name(sliced.name, profile), a)


def changes(sliced: SlicedFile, profile: HardwareProfile, settings: LoopSettings) -> dict:
    """Exactly what ClearLoop adds to the first loop, for people who want to read the G-code."""
    a = analyze(sliced, profile, settings)
    if a.problems:
        raise ProcessError("; ".join(a.problems))
    gcode = clean_input(sliced)
    body = _loop_text(
        gcode, profile, settings, a.plan, sliced.info.printer_model_id == printers.P2S, 1, settings.loops == 1
    )
    blocks = [{"name": m.group(1), "gcode": m.group(0)} for m in BLOCK_RE.finditer(body)]
    return {
        "blocks": blocks,
        "loops": settings.loops,
        "lines_added": sum(b["gcode"].count("\n") for b in blocks),
    }


def compatibility(first: SlicedFile, other: SlicedFile) -> list[str]:
    """Reasons two plates can't share one file. The printer takes its filament list from the first plate."""
    a, b = first.info, other.info
    out = []
    if a.printer_model_id != b.printer_model_id:
        out.append(
            f"{other.name} was sliced for a different printer ({printers.model_name(b.printer_model_id)})"
        )
    if a.filament_slots != b.filament_slots:
        out.append(
            f"{other.name} uses different filaments or slots ({', '.join(f'{t} {c}' for _, t, c in b.filament_slots)} "
            f"vs {', '.join(f'{t} {c}' for _, t, c in a.filament_slots)}). The printer maps filaments once per "
            "file, so every plate in a batch must use the same filament slots"
        )
    if a.bed_type != b.bed_type:
        out.append(f"{other.name} is sliced for a different build plate ({b.bed_type} vs {a.bed_type})")
    if a.nozzle != b.nozzle:
        if not a.nozzle or not b.nozzle:
            out.append(
                f"{other.name if not b.nozzle else first.name} doesn't say which nozzle it was sliced for"
            )
        else:
            out.append(f"{other.name} is sliced for a different nozzle ({b.nozzle} vs {a.nozzle} mm)")
    return out


def process_batch(items: list[tuple], profile: HardwareProfile, settings: LoopSettings) -> Result:
    """Several plates in one file, e.g. plate A three times, then plate B twice.

    ``items``: (plate, count) or (plate, count, per-part push heights for that plate). Each plate is
    analysed and swept on its own; the settings' own per-part heights belong to no plate and are ignored.
    """
    items = [(it[0], it[1], dict(it[2]) if len(it) > 2 and it[2] else {}) for it in items if it[1] > 0]
    if not items:
        raise ProcessError("add at least one plate with a count of 1 or more")
    first = items[0][0]
    problems = [p for sf, _, _ in items[1:] for p in compatibility(first, sf)]
    if problems:
        raise ProcessError("; ".join(problems))
    total = sum(n for _, n, _ in items)
    run = replace(settings, loops=total)
    plans = []
    for sf, _, pp in items:
        own = replace(run, sweep=replace(run.sweep, part_push_z=pp))
        an = analyze(sf, profile, own)
        if an.problems:
            raise ProcessError(f"{sf.name}: " + "; ".join(an.problems))
        plans.append((own, an))
    is_p2s = first.info.printer_model_id == printers.P2S
    bodies, k = [], 0
    for (sf, n, _), (own, an) in zip(items, plans, strict=True):
        gcode = clean_input(sf)
        for _ in range(n):
            k += 1
            bodies.append(_loop_text(gcode, profile, own, an.plan, is_p2s, k, k == total))
    text = _join_loops(bodies)
    base = (first.name or "plate.gcode.3mf").rsplit("/", 1)[-1]
    name = output_name(base if len(items) == 1 else f"batch-{total}x-{base}", profile)
    return Result(first.to_bytes(text), text, name, plans[0][1])
