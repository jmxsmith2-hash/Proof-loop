"""Streaming G-code parser: every move as columns, plus temperatures, fans and the slicer's own notes.

Understands Bambu Studio / OrcaSlicer, PrusaSlicer and Cura output: layer markers (`; CHANGE_LAYER`,
`;LAYER_CHANGE`, `;LAYER:n`), feature markers (`; FEATURE:`, `;TYPE:`), header and config comments,
absolute or relative extrusion (M82/M83), G92 resets, arcs (G2/G3, split into short segments),
firmware retraction (G10/G11), dwell (G4), acceleration (M204) and Bambu's `M106 P1` part fan.
Moves before the first layer marker are the printer's start code (purge lines, wipes), kept but
flagged so the checks can leave them out.
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any
from xml.etree import ElementTree as ET

import numpy as np

from proofloop.core.config import ConfigError

LAYER_MARKERS = ("; CHANGE_LAYER", ";LAYER_CHANGE", ";LAYER:")
# Bambu Studio's file layout. A loop file (the FarmLoop app's or ProofLoop's) repeats the executable block once per
# loop; between the start code's end and the end code's start is the print itself.
EXEC_START = "; EXECUTABLE_BLOCK_START"
START_END = "; MACHINE_START_GCODE_END"
END_START = "; MACHINE_END_GCODE_START"
PHASES = ("start", "print", "end")
FEATURE_PREFIXES = ("; FEATURE:", ";TYPE:")
ARC_SEGMENT_DEG = 10.0
_KV = re.compile(r"^;\s*([A-Za-z_][\w \[\]^/().-]*?)\s*[=:]\s*(.*?)\s*$")


@dataclass
class GcodeTrace:
    """All motion of one print as numpy columns (one row per move segment)."""
    x0: np.ndarray
    y0: np.ndarray
    x1: np.ndarray
    y1: np.ndarray
    z: np.ndarray            # Z at the end of the move
    length: np.ndarray       # 3D length, mm
    de: np.ndarray           # filament fed, mm (negative = retraction)
    feed: np.ndarray         # commanded speed, mm/s
    accel: np.ndarray        # mm/s², from M204 (0 = unknown)
    layer: np.ndarray        # -1 = start code before the first layer marker
    feature: np.ndarray      # index into `features`
    features: list[str]
    header: dict[str, str]
    loop: np.ndarray | None = None      # loop number per move (0 = before the first executable block)
    phase: np.ndarray | None = None     # index into PHASES per move, when the file marks them
    loops: int = 0                      # executable blocks (1 for a normal Bambu file)
    phase_marked: bool = False
    header_all: dict[str, list[str]] = field(default_factory=dict)   # every occurrence (one per loop)
    marks: dict[str, int] = field(default_factory=dict)              # ProofLoop / FarmLoop blocks, pauses
    dwell_events: list[tuple[int, float]] = field(default_factory=list)   # (move index, seconds)
    path: str = ""
    nozzle_events: list[tuple[int, float, bool]] = field(default_factory=list)   # (move index, target C, waits)
    bed_events: list[tuple[int, float]] = field(default_factory=list)
    fan_events: list[tuple[int, float]] = field(default_factory=list)            # part fan 0..1
    firmware_retracts: int = 0
    dwell_s: float = 0.0
    tool_changes: int = 0
    homed_before_print: bool = False
    layer_markers: int = 0
    lines: int = 0
    source: str = ""
    plate: int | None = None
    plate_info: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.x0)


def _num(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def parse_stream(stream: IO[str], source: str = "") -> GcodeTrace:
    # The hot loop stores only each move's end point, filament and speed. Everything that changes
    # rarely (layer, feature, acceleration, position resets) is kept as (move index, value) events
    # and spread over the moves afterwards with numpy.
    xs, ys, zs, des, feeds = array("d"), array("d"), array("d"), array("d"), array("d")
    ax, ay, az, ade, afeed = xs.append, ys.append, zs.append, des.append, feeds.append
    layer_ev: list[tuple[int, int]] = []
    loop_ev: list[tuple[int, int]] = []
    phase_ev: list[tuple[int, int]] = []
    header_all: dict[str, list[str]] = {}
    marks = {"proofloop_blocks": 0, "farmloop_sections": 0, "loop_pauses": 0}
    dwell_ev: list[tuple[int, float]] = []
    loop = 0
    feat_ev: list[tuple[int, int]] = []
    accel_ev: list[tuple[int, float]] = []
    reset_ev: list[tuple[int, float, float, float]] = []   # the move at this index starts from (x, y, z)

    features: list[str] = ["start"]
    feature_ids = {"start": 0}
    header: dict[str, str] = {}
    nozzle_ev: list[tuple[int, float, bool]] = []
    bed_ev: list[tuple[int, float]] = []
    fan_ev: list[tuple[int, float]] = []
    x = y = z = e = 0.0
    feed = 25.0
    abs_xyz, e_mode_abs = True, True      # E is absolute only under G90 + M82 (Klipper/Bambu semantics)
    layer = -1
    n_lines = fw_retracts = tool_changes = markers = 0
    dwell = 0.0
    homed = False
    first_layer_seen = False

    for raw in stream:
        n_lines += 1
        line = raw.strip()
        if not line:
            continue
        if line[0] == ";":
            if line.startswith(LAYER_MARKERS):
                if line.startswith(";LAYER:") and line[7:].strip().lstrip("-").isdigit() and int(line[7:]) < 0:
                    continue          # Cura raft layers are numbered negative; keep them in the start block
                layer += 1
                markers += 1
                first_layer_seen = True
                layer_ev.append((len(xs), layer))
                continue
            if line.startswith(EXEC_START):
                loop += 1
                loop_ev.append((len(xs), loop))
                phase_ev.append((len(xs), 0))
                continue
            if line.startswith(START_END):
                phase_ev.append((len(xs), 1))
                continue
            if line.startswith(END_START):
                phase_ev.append((len(xs), 2))
                continue
            if line.startswith("; >>> PROOFLOOP"):
                marks["proofloop_blocks"] += 1
                continue
            if line.startswith("; @fl:section:start"):
                marks["farmloop_sections"] += 1
                continue
            if line.startswith(FEATURE_PREFIXES):
                name = line.split(":", 1)[1].strip().lower()
                if name not in feature_ids:
                    feature_ids[name] = len(features)
                    features.append(name)
                feat_ev.append((len(xs), feature_ids[name]))
                continue
            m = _KV.match(line)
            if m and len(m.group(2)) < 300:
                key = m.group(1).strip().lower()
                header.setdefault(key, m.group(2))
                header_all.setdefault(key, []).append(m.group(2))
            continue
        cut = line.find(";")
        if cut >= 0:
            line = line[:cut]
        parts = line.upper().split()
        if not parts:
            continue
        cmd = parts[0]
        if cmd == "G1" or cmd == "G0" or cmd == "G2" or cmd == "G3":
            nx, ny, nz, ne = x, y, z, e
            ci = cj = r = None
            for p in parts[1:]:
                try:
                    val = float(p[1:])
                except ValueError:
                    continue
                key = p[0]
                if key == "X":
                    nx = val if abs_xyz else x + val
                elif key == "Y":
                    ny = val if abs_xyz else y + val
                elif key == "E":
                    ne = val if (abs_xyz and e_mode_abs) else e + val
                elif key == "Z":
                    nz = val if abs_xyz else z + val
                elif key == "F":
                    if val > 0:
                        feed = val / 60.0
                elif key == "I":
                    ci = val
                elif key == "J":
                    cj = val
                elif key == "R":
                    r = val
            if cmd in ("G2", "G3") and (ci is not None or cj is not None or r is not None):
                for sx, sy, sz, se in _arc(x, y, z, e, nx, ny, nz, ne, ci, cj, r, cmd == "G2"):
                    ax(sx), ay(sy), az(sz), ade(se - e), afeed(feed)
                    x, y, z, e = sx, sy, sz, se
            elif nx != x or ny != y or nz != z or ne != e:
                ax(nx), ay(ny), az(nz), ade(ne - e), afeed(feed)
            x, y, z, e = nx, ny, nz, ne
        elif cmd == "G92":
            moved = False
            for p in parts[1:]:
                val = _num(p[1:])
                if val is None:
                    continue
                key = p[0]
                if key == "E":
                    e = val
                elif key in "XYZ":
                    moved = True
                    if key == "X":
                        x = val
                    elif key == "Y":
                        y = val
                    else:
                        z = val
            if moved:
                reset_ev.append((len(xs), x, y, z))
        elif cmd == "M82":
            e_mode_abs = True
        elif cmd == "M83":
            e_mode_abs = False
        elif cmd == "G90":
            abs_xyz = True
        elif cmd == "G91":
            abs_xyz = False
        elif cmd in ("M104", "M109"):
            val = _param(parts, "S")
            if val is None:
                val = _param(parts, "R")
            if val is not None and _param(parts, "T") in (None, 0.0):
                nozzle_ev.append((len(xs), val, cmd == "M109"))
        elif cmd in ("M140", "M190"):
            val = _param(parts, "S")
            if val is None:
                val = _param(parts, "R")
            if val is not None:
                bed_ev.append((len(xs), val))
        elif cmd == "M106":
            if _param(parts, "P") in (None, 0.0, 1.0):          # Bambu: P1 part fan, P2 aux, P3 chamber
                val = _param(parts, "S")
                fan_ev.append((len(xs), min(1.0, (val if val is not None else 255.0) / 255.0)))
        elif cmd == "M107":
            if _param(parts, "P") in (None, 0.0, 1.0):
                fan_ev.append((len(xs), 0.0))
        elif cmd == "M204":
            val = _param(parts, "S") or _param(parts, "P")
            if val:
                accel_ev.append((len(xs), val))
        elif cmd == "M400" and len(parts) > 1 and parts[1] == "U1":
            marks["loop_pauses"] += 1              # Bambu's pause: a loop file waiting for its plate check
        elif cmd == "M400" and len(parts) > 1 and parts[1].startswith("S"):
            wait = _num(parts[1][1:]) or 0.0       # Bambu's dwell in seconds
            dwell += wait
            dwell_ev.append((len(xs), wait))
        elif cmd == "G4":
            p, sec = _param(parts, "P"), _param(parts, "S")
            wait = (p or 0.0) / 1000.0 + (sec or 0.0)
            dwell += wait
            dwell_ev.append((len(xs), wait))
        elif cmd == "G28":
            if not first_layer_seen:
                homed = True
        elif cmd == "G10" and len(parts) == 1:
            fw_retracts += 1
        elif cmd[0] == "T" and cmd[1:].isdigit() and int(cmd[1:]) < 64 and first_layer_seen:
            tool_changes += 1
        elif cmd == "M620" and len(parts) > 1 and parts[1].startswith("S") and first_layer_seen:
            tool_changes += 1                      # Bambu AMS filament change block

    n = len(xs)
    x1, y1, z1 = (np.frombuffer(c, dtype=np.float64) if n else np.zeros(0) for c in (xs, ys, zs))
    x0, y0, z0 = (np.concatenate([[0.0], c[:-1]]) if n else np.zeros(0) for c in (x1, y1, z1))
    for i, rx, ry, rz in reset_ev:
        if i < n:
            x0[i], y0[i], z0[i] = rx, ry, rz
    length = np.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2)
    return GcodeTrace(x0=x0, y0=y0, x1=x1, y1=y1, z=z1, length=length,
                      de=np.frombuffer(des, dtype=np.float64) if n else np.zeros(0),
                      feed=np.frombuffer(feeds, dtype=np.float64) if n else np.zeros(0),
                      accel=_spread(accel_ev, n, 0.0), layer=_spread(layer_ev, n, -1).astype(np.int32),
                      feature=_spread(feat_ev, n, 0).astype(np.int32), features=features, header=header,
                      nozzle_events=nozzle_ev, bed_events=bed_ev, fan_events=fan_ev, firmware_retracts=fw_retracts,
                      dwell_s=dwell, tool_changes=tool_changes, homed_before_print=homed, layer_markers=markers,
                      lines=n_lines, source=source,
                      loop=_spread(loop_ev, n, 0).astype(np.int32),
                      phase=_spread(phase_ev, n, 1).astype(np.int8) if phase_ev else None,
                      loops=loop, phase_marked=any(p == 1 for _, p in phase_ev), header_all=header_all, marks=marks,
                      dwell_events=dwell_ev)


def _spread(events: list[tuple], n: int, default: float) -> np.ndarray:
    """Per-move values from (first move index, value) events."""
    if not events:
        return np.full(n, default, dtype=float)
    idx = np.array([ev[0] for ev in events])
    val = np.array([ev[1] for ev in events], dtype=float)
    pos = np.searchsorted(idx, np.arange(n), side="right") - 1
    return np.where(pos >= 0, val[np.clip(pos, 0, None)], default)


def _param(parts: list[str], key: str) -> float | None:
    for p in parts[1:]:
        if p[:1].upper() == key:
            return _num(p[1:])
    return None


def _arc(x, y, z, e, nx, ny, nz, ne, ci, cj, r, clockwise):
    """Split a G2/G3 arc into short straight segments (I/J centre offsets or R radius)."""
    if ci is None and cj is None and r is not None:      # R form: find the centre
        dx, dy = nx - x, ny - y
        d = math.hypot(dx, dy)
        if d == 0 or abs(r) < d / 2:
            return [(nx, ny, nz, ne)]
        h = math.sqrt(r * r - d * d / 4) * (1 if (r > 0) != clockwise else -1)
        cx, cy = x + dx / 2 - h * dy / d, y + dy / 2 + h * dx / d
    else:
        cx, cy = x + (ci or 0.0), y + (cj or 0.0)
    radius = math.hypot(x - cx, y - cy)
    a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
    sweep = a1 - a0
    if clockwise and sweep >= 0:
        sweep -= 2 * math.pi
    elif not clockwise and sweep <= 0:
        sweep += 2 * math.pi
    n = max(1, int(abs(math.degrees(sweep)) / ARC_SEGMENT_DEG + 0.999))
    if radius == 0:
        return [(nx, ny, nz, ne)]
    out = []
    for i in range(1, n + 1):
        t = i / n
        if i == n:
            out.append((nx, ny, nz, ne))
        else:
            a = a0 + sweep * t
            out.append((cx + radius * math.cos(a), cy + radius * math.sin(a), z + (nz - z) * t, e + (ne - e) * t))
    return out


def read_gcode(path: Path | str, plate: int | None = None) -> GcodeTrace:
    """Parse a .gcode file or one plate of a Bambu Studio .gcode.3mf."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"no such file: {path}")
    if zipfile.is_zipfile(path):
        trace = _read_3mf_plate(path, plate)
    else:
        with open(path, encoding="utf-8", errors="replace") as f:
            trace = parse_stream(f, source=path.name)
    trace.path = str(path)
    return trace


def _read_3mf_plate(path: Path, plate: int | None) -> GcodeTrace:
    with zipfile.ZipFile(path) as z:
        plates = sorted((int(m.group(1)), n) for n in z.namelist()
                        if (m := re.match(r"^/?Metadata/plate_(\d+)\.gcode$", n)))
        if not plates:
            raise ConfigError(f"{path.name} has no sliced plates (G-code). Slice it in Bambu Studio and use "
                              "'Export plate sliced file', or check the 3MF model with `proofloop check design`")
        chosen = next((p for p in plates if p[0] == plate), None) if plate else plates[0]
        if chosen is None:
            raise ConfigError(f"{path.name} has plates {[p[0] for p in plates]}, not {plate}")
        with z.open(chosen[1]) as raw:
            trace = parse_stream(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"),
                                 source=f"{path.name} (plate {chosen[0]})")
        trace.plate = chosen[0]
        trace.plate_info = _slice_info(z, chosen[0]) if "Metadata/slice_info.config" in z.namelist() else {}
        trace.plate_info["plates"] = [p[0] for p in plates]
    return trace


def _slice_info(z: zipfile.ZipFile, plate: int) -> dict[str, Any]:
    try:
        root = ET.fromstring(z.read("Metadata/slice_info.config"))
    except ET.ParseError:
        return {}
    for p in root.iter("plate"):
        meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
        if str(meta.get("index", "")) != str(plate):
            continue
        filaments = [dict(f.attrib) for f in p.findall("filament")]
        return {"prediction_s": _num(meta.get("prediction") or ""), "weight_g": _num(meta.get("weight") or ""),
                "printer_model_id": meta.get("printer_model_id"), "nozzle_diameters": meta.get("nozzle_diameters"),
                "filaments": filaments}
    return {}
