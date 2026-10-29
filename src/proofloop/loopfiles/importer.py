"""Learn a hardware profile from a FarmLoop file.

Give ProofLoop a plain slice and the FarmLoop app's processed version of the
same plate (``FL_S1_...`` or ``FL_S2_...``). Comparing the two shows exactly
which lines the FarmLoop app added for your printer and kit. ProofLoop keeps
the hardware-specific parts verbatim (FarmBoard triggers and switch gestures,
bend steps and waits, the sweep path and park position) and drops the parts it
replaces with its own logic (cooldown, sweep heights). Known defects in the
vendor output, such as a natural-cooldown wait placed before the print, are
detected and left out.

Nothing is inferred beyond what the two files contain. If a part of the file
can't be classified with certainty, the import stops and says what it found.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field

from proofloop.core import printers
from proofloop.loopfiles.gcode.routines import generalize_anchor
from proofloop.loopfiles.profiles import HardwareProfile, SweepGeometry, SweepPass, slug
from proofloop.loopfiles.threemf import SlicedFile, layout

SENTINEL = re.compile(r"^M104\s+S([1-5])(?:\s|;|$)")
COOLDOWN_WAIT = re.compile(r"^M190\s+S(\d+(?:\.\d+)?)")
FAN_ON = re.compile(r"^M106\b.*\bS(?!0\b)\d+")
FAN_OFF = re.compile(r"^M106\b(?:\s+P\d+)?\s+S0\b")
GESTURE_LINE = re.compile(r"^(G1\s+Z[-\d.]+(\s+F\d+)?|G4\s+[PS][\d.]+|M400)\b")
MOVE = re.compile(r"^G[01]\b")
WORD = re.compile(r"([XYZF])\s*([-+]?\d+\.?\d*)")


class ImportError_(ValueError):
    pass


@dataclass
class ImportReport:
    profile: HardwareProfile
    found: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def _first_loop(sf: SlicedFile) -> list[str]:
    lay = layout(sf.gcode)
    if not lay.ok:
        raise ImportError_(f"{sf.name or 'file'}: missing slicer markers {lay.missing()}")
    return lay.lines[: lay.exec_end + 1]


def _strip_block(lines: list[str]) -> str:
    """Drop FarmLoop app bookkeeping comments and surrounding blank lines; keep everything else verbatim."""
    kept = [ln for ln in lines if not ln.lstrip().startswith("; @fl:")]
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and (not kept[-1].strip() or (kept[-1].lstrip().startswith(";") and not _code(kept[-1]))):
        kept.pop()
    return "".join(kept)


def _sentinel_block(lines: list[str], i: int) -> tuple[str, int]:
    """A FarmBoard step: the comment above the sentinel, the sentinel, and its switch gesture and waits."""
    start = i
    while start > 0 and lines[start - 1].lstrip().startswith("; ---"):
        start -= 1
    j = i + 1
    while j < len(lines):
        s = lines[j].strip()
        if "@fl:section:start" in s and "sweep" in s or "PUSH SECTION" in s:
            break
        if (
            SENTINEL.match(s)
            or s.startswith("; ---")
            and j + 1 < len(lines)
            and SENTINEL.match(lines[j + 1].strip())
        ):
            break
        code = _code(s)
        if code and not GESTURE_LINE.match(code):
            break
        j += 1
    return _strip_block(lines[start:j]), j


def import_profile(raw: SlicedFile, farmloop: SlicedFile, name: str | None = None) -> ImportReport:
    if raw.info.printer_model_id != farmloop.info.printer_model_id:
        raise ImportError_(
            "the two files were sliced for different printers "
            f"({printers.model_name(raw.info.printer_model_id)} vs {printers.model_name(farmloop.info.printer_model_id)})"
        )
    a = _first_loop(raw)
    b = _first_loop(farmloop)
    found: list[str] = []
    dropped: list[str] = []

    # ---- start: what FarmLoop inserted before the print --------------------------------
    la, lb = layout(raw.gcode), layout(farmloop.gcode)
    sa, sb = a[la.exec_start : la.start_gcode_end], b[lb.exec_start : lb.start_gcode_end]
    sm = difflib.SequenceMatcher(None, sa, sb, autojunk=False)
    start_block, start_anchor, template_line = "", None, ""
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        added = sb[j1:j2]
        text = "".join(added)
        g4_runs = sum(1 for ln in added if _code(ln).startswith("G4 S"))
        if "NATURAL COOLDOWN" in text or g4_runs >= 10:
            dropped.append(
                f"a {g4_runs}-dwell natural-cooldown wait the FarmLoop app placed BEFORE the print "
                "(heaters on, fans off). ProofLoop never copies it; natural cooldown runs after the print."
            )
            continue
        if op == "insert" and any(
            SENTINEL.match(_code(ln)) or _code(ln).startswith(("G28", "G1 Z")) for ln in added
        ):
            if start_block:
                raise ImportError_(
                    "found more than one hardware insertion in the start G-code; can't tell them apart"
                )
            anchor_line = sa[i1 - 1].rstrip("\r\n") if i1 > 0 else ""
            if not anchor_line.strip():
                raise ImportError_("the FarmLoop start insertion has no unique line before it to anchor to")
            start_anchor = generalize_anchor(anchor_line)
            template_line = anchor_line.strip()
            start_block = _strip_block(added)
            found.append(f"start step: {len(added)} lines inserted after '{anchor_line.strip()}'")
        else:
            dropped.append(
                f"slicer-setting change in the start G-code ({i2 - i1} lines replaced by {j2 - j1}); "
                "ProofLoop keeps your slicer's original start G-code"
            )

    # ---- end: FarmLoop's ejection cycle ---------------------------------------------------
    end = b[lb.end_gcode_start : lb.exec_end]
    sentinels = {int(m.group(1)): i for i, ln in enumerate(end) if (m := SENTINEL.match(_code(ln)))}
    sweep_start = next(
        (i for i, ln in enumerate(end) if "@fl:section:start" in ln and "sweep" in ln),
        next((i for i, ln in enumerate(end) if "PUSH SECTION" in ln), -1),
    )
    if sweep_start < 0:
        raise ImportError_(
            "could not find the sweep section in the FarmLoop file (no '@fl:section:start ...sweep' or "
            "'PUSH SECTION' marker). Please open an issue with both files so support can be added."
        )
    sweep_end = next((i for i in range(sweep_start, len(end)) if "END SECTION" in end[i]), -1)
    if sweep_end < 0:
        sweep_end = next(
            (i for i in range(sweep_start + 1, len(end)) if SENTINEL.match(_code(end[i]))), len(end)
        )

    waits = [(i, COOLDOWN_WAIT.match(_code(ln))) for i, ln in enumerate(end)]
    waits = [(i, m) for i, m in waits if m]
    cooldown_repeat = len(waits)
    if cooldown_repeat:
        found.append(
            f"bed-temperature wait repeated {cooldown_repeat}x (target {waits[0][1].group(1)} C in the file)"
        )

    cooldown_fan = []
    first_wait = waits[0][0] if waits else sweep_start
    for ln in end[:first_wait]:
        if FAN_ON.match(_code(ln)):
            cooldown_fan.append(ln.rstrip("\r\n"))
    if cooldown_fan:
        found.append(f"cooldown fan: {', '.join(_code(f) for f in cooldown_fan)}")

    door_open = bend = safety = door_close = ""
    if 2 in sentinels:
        door_open, _ = _sentinel_block(end, sentinels[2])
        found.append("FarmBoard T1 (door open + fan)")
    if 3 in sentinels:
        bend, _ = _sentinel_block(end, sentinels[3])
        found.append("FarmBoard T2 (bend) with its wait")
    if 4 in sentinels:
        safety, _ = _sentinel_block(end, sentinels[4])
        found.append("FarmBoard safety check with its wait")
    if 5 in sentinels:
        door_close, _ = _sentinel_block(end, sentinels[5])
        found.append("FarmBoard T3 (door close)")

    if not sentinels:
        # Stage 1: no electronics. Motion between the cooldown and the sweep is the kit's own
        # bend step (e.g. the static bender driven by the printer's Z axis). Keep it verbatim.
        after_wait = (waits[-1][0] + 1) if waits else 0
        pre = [
            ln
            for ln in end[after_wait:sweep_start]
            if _code(ln) and (MOVE.match(_code(ln)) or _code(ln).startswith(("G4", "M400")))
        ]
        if pre:
            bend = "".join(pre)
            found.append(f"Stage 1 bend step: {len(pre)} motion/dwell lines before the sweep")

    sweep = _parse_sweep(end[sweep_start:sweep_end])
    park_lines = []
    stop = min([v for v in sentinels.values() if v > sweep_end] or [len(end)])
    for ln in end[sweep_end + 1 : stop]:
        c = _code(ln)
        if MOVE.match(c) and ("X" in c or "Y" in c) and "Z" not in c:
            park_lines.append(c)
    sweep.park = park_lines
    found.append(
        f"sweep: {len(sweep.passes)} passes, lanes X{min(sweep.lanes):g}-X{max(sweep.lanes):g} "
        f"(pitch {sweep.lane_pitch:g} mm), Y{sweep.y_rear:g} -> Y{sweep.y_front:g}"
    )

    tail_from = max([sentinels.get(5, 0), sweep_end])
    fans_off = [ln.rstrip("\r\n") for ln in end[tail_from:] if FAN_OFF.match(_code(ln))]

    stage = 2 if sentinels else 1
    model = printers.model_name(raw.info.printer_model_id)
    pid = slug(name or f"{model}-stage{stage}-imported")
    profile = HardwareProfile(
        id=pid,
        name=name or f"{model} Stage {stage} (imported)",
        printer_model_id=raw.info.printer_model_id,
        stage=stage,
        description=f"Learned from {farmloop.name or 'a FarmLoop file'}",
        source={
            "kind": "imported",
            "farmloop_file": farmloop.name,
            "farmloop_gcode_sha256": hashlib.sha256(farmloop.gcode.encode()).hexdigest(),
            "plain_file": raw.name,
            "slicer": raw.info.slicer,
            "start_template_line": template_line,
        },
        sweep=sweep,
        start_anchor=start_anchor,
        start_block=start_block,
        door_open=door_open,
        bend=bend,
        safety=safety,
        door_close=door_close,
        cooldown_fan=cooldown_fan,
        cooldown_repeat=max(1, cooldown_repeat),
        fans_off=fans_off,
    )
    return ImportReport(profile, found, dropped)


def _parse_sweep(lines: list[str]) -> SweepGeometry:
    x = y = None
    feed = None
    z_feed = None
    passes: list[SweepPass] = []
    ys: list[float] = []
    for ln in lines:
        c = _code(ln)
        if not MOVE.match(c):
            continue
        w = {k: float(v) for k, v in WORD.findall(c)}
        if "Z" in w and "F" in w and "X" not in w and "Y" not in w:
            z_feed = int(w["F"])
            continue
        f = int(w["F"]) if "F" in w else feed
        if "X" in w:
            x = w["X"]
        if "Y" in w:
            ny = w["Y"]
            ys.append(ny)
            if y is not None and ny < y and x is not None:
                # A push: rear to front at the current lane.
                if not passes or passes[-1].feed != f:
                    passes.append(SweepPass(int(f or 0), []))
                passes[-1].lanes.append(x)
            y = ny
        feed = f
    if not passes or not ys:
        raise ImportError_("the sweep section has no rear-to-front pushes; can't learn the sweep path")
    return SweepGeometry(y_rear=max(ys), y_front=min(ys), passes=passes, z_feed=z_feed or 1200)
