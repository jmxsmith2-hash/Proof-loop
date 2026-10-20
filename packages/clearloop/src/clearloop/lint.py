"""Check any loop file (FarmLoop app output or ClearLoop output) before you print it.

Every check is tied to a documented problem:

* ``M190 R`` raised HMS 0300-0100-0003-0008 on a P2S and drove the bed target up.
* The FarmLoop app (2.72) put a 2-hour wait before the print with the heaters on.
* Bambu firmware does not run G-code after ``; EXECUTABLE_BLOCK_END``.
* Stock end G-code ends with ``M18``; moving afterwards without ``M17`` commands
  disabled steppers.
* One sweep height for the whole plate misses short parts.
* Bambu firmware checks ``plate_N.gcode.md5`` before it starts a plate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import printers
from .gcode.objects import read_parts
from .threemf import EXEC_END, EXEC_START, SlicedFile

M190R = re.compile(r"^\s*M190\s+R")
DWELL = re.compile(r"^\s*(?:G4\s+([PS])(\d+(?:\.\d+)?)|M400\s+S(\d+(?:\.\d+)?))")
HEAT = re.compile(r"^\s*M1(?:04|09|40|90)\s+S(\d+(?:\.\d+)?)")
STEP = re.compile(r"^\s*(M17|M18|M84)\b")
MOVE = re.compile(r"^\s*G[01]\b")
WORD = re.compile(r"([XYZ])\s*([-+]?\d+\.?\d*)")
SENTINEL = re.compile(r"^\s*M104\s+S[1-5](?:\s|;|$)")


@dataclass
class Finding:
    level: str  # "danger", "warning", "info", "ok"
    title: str
    detail: str

    def as_dict(self) -> dict:
        return self.__dict__


def _code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def _loops(lines: list[str]) -> list[tuple[int, int]]:
    out, start = [], None
    for i, ln in enumerate(lines):
        s = ln.rstrip("\r\n")
        if s == EXEC_START:
            start = i
        elif s == EXEC_END and start is not None:
            out.append((start, i))
            start = None
    return out


def lint(sf: SlicedFile) -> list[Finding]:
    f: list[Finding] = []
    lines = sf.gcode.splitlines()
    loops = _loops(lines)

    if not sf.md5_ok:
        f.append(
            Finding(
                "danger",
                "Checksum doesn't match",
                "plate_N.gcode.md5 doesn't match the G-code; the printer will refuse or reject the plate.",
            )
        )
    if not loops:
        f.append(
            Finding(
                "danger",
                "No executable block",
                "The file has no '; EXECUTABLE_BLOCK_START/END' pair; the printer won't run it.",
            )
        )
        return f

    # 1. M190 R
    bad = [i + 1 for i, ln in enumerate(lines) if M190R.match(ln)]
    if bad:
        f.append(
            Finding(
                "danger",
                "Uses M190 R",
                f"Line {bad[0]} ({len(bad)} total). On a P2S this raised HMS 0300-0100-0003-0008 and drove the bed target up instead of waiting for it to cool. Use M190 S.",
            )
        )

    # 2. Long waits before the print starts, with heaters on.
    for n, (a, _b) in enumerate(loops, 1):
        start_end = next(
            (i for i in range(a, len(lines)) if lines[i].startswith("; MACHINE_START_GCODE_END")), None
        )
        if start_end is None:
            continue
        heat_on, waited, worst = False, 0.0, 0.0
        for ln in lines[a:start_end]:
            c = _code(ln)
            if m := HEAT.match(c):
                heat_on = float(m.group(1)) > 0
            if m := DWELL.match(c):
                secs = float(m.group(2)) / 1000 if m.group(1) == "P" else float(m.group(2) or m.group(3))
                if heat_on:
                    waited += secs
                    worst = max(worst, waited)
        if worst >= 600:
            f.append(
                Finding(
                    "danger",
                    f"Loop {n}: {worst / 60:.0f} min wait before printing, heaters on",
                    "The start G-code dwells with a heater set. This is the FarmLoop app 2.72 'natural cooldown' "
                    "placement bug: the printer looks frozen and stays hot. Don't run this file.",
                )
            )
        if any("FL NATURAL COOLDOWN" in ln for ln in lines[a:start_end]):
            f.append(
                Finding(
                    "danger",
                    f"Loop {n}: natural cooldown placed before the print",
                    "A post-print cooldown block sits in the start G-code.",
                )
            )

    # 3. Code after the last executable block.
    last_end = loops[-1][1]
    stray = [i + 1 for i in range(last_end + 1, len(lines)) if _code(lines[i])]
    if stray:
        f.append(
            Finding(
                "warning",
                "G-code after the last executable block",
                f"{len(stray)} command lines after line {last_end + 1} won't run (Bambu firmware ignores code after '; EXECUTABLE_BLOCK_END').",
            )
        )

    # 4. Motion while steppers are off.
    for n, (a, b) in enumerate(loops, 1):
        off_at = None
        for i in range(a, b):
            c = _code(lines[i])
            if m := STEP.match(c):
                off_at = i if m.group(1) in ("M18", "M84") else None
            elif off_at is not None and MOVE.match(c) and WORD.search(c):
                f.append(
                    Finding(
                        "danger",
                        f"Loop {n}: moves after steppers were switched off",
                        f"Line {i + 1} moves an axis after M18/M84 on line {off_at + 1} with no M17 in between.",
                    )
                )
                break

    # 5. Parts below every sweep pass.
    info = sf.info
    first = "\n".join(lines[: loops[0][1] + 1])
    parts = read_parts(first, info.object_names, info.object_heights)
    end_start = next(
        (i for i in range(loops[0][0], loops[0][1]) if lines[i].startswith("; MACHINE_END_GCODE_START")), None
    )
    pushes: list[tuple[float, float]] = []  # (x, z) of rear-to-front moves
    if parts and end_start is not None:
        x = y = z = None
        for ln in lines[end_start : loops[0][1]]:
            c = _code(ln)
            if not MOVE.match(c):
                continue
            w = {k: float(v) for k, v in WORD.findall(c)}
            x, z = w.get("X", x), w.get("Z", z)
            if "Y" in w:
                if y is not None and w["Y"] < y - 50 and x is not None and z is not None:
                    pushes.append((x, z))
                y = w["Y"]
        if pushes:
            missed = []
            for p in parts:
                # 15 mm = half the 30 mm lane spacing of FarmLoop's P2S sweep.
                reach = [z for (px, z) in pushes if p.xmin - 15 <= px <= p.xmax + 15]
                if reach and min(reach) >= p.height:
                    missed.append(f"{p.name} ({p.height:.1f} mm, lowest push Z{min(reach):g})")
            if missed:
                f.append(
                    Finding(
                        "danger",
                        "Parts the sweep passes over",
                        "Every sweep pass near these parts is higher than the part, so they'll stay on the plate: "
                        + "; ".join(missed)
                        + ". This is the single-push-height problem; ClearLoop pushes each part at its own height.",
                    )
                )
            else:
                f.append(
                    Finding(
                        "ok",
                        "Every part is reached by a sweep pass",
                        f"{len(parts)} part(s) checked against {len(pushes)} pushes.",
                    )
                )

    if parts and end_start is not None and not pushes:
        f.append(
            Finding(
                "info",
                "No ejection cycle",
                "This looks like a plain slice: there's no sweep after the print.",
            )
        )

    # 6. FarmBoard naming.
    has_sentinels = any(SENTINEL.match(ln) for ln in lines[loops[0][0] : loops[0][1]])
    if has_sentinels and not (sf.name or "").startswith("FL_S2_"):
        f.append(
            Finding(
                "info",
                "FarmBoard file without the FL_S2_ prefix",
                "FarmLoop's documentation says FarmBoard kits key off the FL_S2_ file-name prefix; renaming may stop the cycle from firing.",
            )
        )

    if not any(x.level in ("danger", "warning") for x in f):
        f.append(
            Finding(
                "ok",
                "No known problems found",
                f"{len(loops)} loop(s) on a {printers.model_name(info.printer_model_id)}.",
            )
        )
    return f
