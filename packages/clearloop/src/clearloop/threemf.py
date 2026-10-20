"""Read and write Bambu Studio / OrcaSlicer ``.gcode.3mf`` plate files.

Everything ClearLoop knows about a plate comes from the file itself: the
printer it was sliced for (``printer_model_id`` in ``slice_info.config``), the
printable area and height (the slicer's own ``CONFIG_BLOCK``), filament types,
object names and per-object heights (``HEADER_BLOCK``). Nothing is assumed.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_PLATE_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")
_HEADER_KV = re.compile(r"^;\s*([^:=]+?)\s*:\s*(.*?)\s*$")
_CONFIG_KV = re.compile(r"^;\s*([a-z0-9_]+)\s*=\s*(.*?)\s*$")

# Markers written by Bambu Studio and OrcaSlicer into every sliced plate.
EXEC_START = "; EXECUTABLE_BLOCK_START"
EXEC_END = "; EXECUTABLE_BLOCK_END"
START_GCODE_END = "; MACHINE_START_GCODE_END"
END_GCODE_START = "; MACHINE_END_GCODE_START"


class PlateFileError(ValueError):
    """The file is not something ClearLoop can safely work with. The message says why."""


@dataclass
class PlateInfo:
    slicer: str = ""
    printer_model_id: str = ""
    printer_model: str = ""
    bed_type: str = ""
    bed_x: float = 0.0
    bed_y: float = 0.0
    printable_height: float = 0.0
    filaments: list[str] = field(default_factory=list)
    filament_grams: float = 0.0
    print_seconds: float = 0.0
    chamber_temp: float = 0.0
    max_z_height: float = 0.0
    object_heights: dict[str, float] = field(default_factory=dict)  # label id -> height (slicer header)
    object_names: dict[str, str] = field(default_factory=dict)  # label id -> name (slice_info)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class SlicedFile:
    """One plate's G-code plus everything needed to write the container back unchanged."""

    gcode: str
    plate: int
    info: PlateInfo
    name: str = ""
    _entries: dict[str, bytes] = field(default_factory=dict, repr=False)
    _order: list[str] = field(default_factory=list, repr=False)

    @property
    def gcode_name(self) -> str:
        return f"Metadata/plate_{self.plate}.gcode"

    @property
    def md5_ok(self) -> bool:
        stored = self._entries.get(self.gcode_name + ".md5", b"").decode(errors="replace").strip()
        return stored.lower() == hashlib.md5(self.gcode.encode()).hexdigest()

    def to_bytes(self, gcode: str, extra: dict[str, bytes] | None = None) -> bytes:
        """Write the plate back. Only the plate G-code, its checksum and ``extra`` entries change."""
        data = gcode.encode()
        entries = dict(self._entries)
        entries[self.gcode_name] = data
        # Bambu firmware checks this checksum before it starts the plate. Bambu Studio
        # and the FarmLoop app both write it as upper-case hex.
        entries[self.gcode_name + ".md5"] = hashlib.md5(data).hexdigest().upper().encode()
        for k, v in (extra or {}).items():
            entries[k] = v
        order = list(self._order)
        for k in entries:
            if k not in order:
                order.append(k)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name in order:
                z.writestr(name, entries[name])
        return buf.getvalue()


def load(path: str | Path, plate: int | None = None) -> SlicedFile:
    path = Path(path)
    return loads(path.read_bytes(), name=path.name, plate=plate)


def loads(raw: bytes, name: str = "", plate: int | None = None) -> SlicedFile:
    if raw[:2] != b"PK":
        raise PlateFileError(
            "this is not a .gcode.3mf file. In Bambu Studio or OrcaSlicer, slice the plate and use "
            "'Export plate sliced file' (.gcode.3mf)."
        )
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            order = z.namelist()
            entries = {n: z.read(n) for n in order}
    except (zipfile.BadZipFile, ValueError, OSError, EOFError) as e:
        raise PlateFileError(f"the file is damaged or not a complete 3MF ({e}); export it again") from e
    plates = sorted(int(m.group(1)) for n in order if (m := _PLATE_RE.match(n)))
    if not plates:
        raise PlateFileError(
            "this 3MF has no sliced G-code. It is a project file; slice it and export the plate "
            "as a sliced file (.gcode.3mf)."
        )
    if plate is None:
        plate = plates[0]
    elif plate not in plates:
        raise PlateFileError(f"plate {plate} is not sliced in this file; sliced plates: {plates}")
    gcode = entries[f"Metadata/plate_{plate}.gcode"].decode("utf-8", errors="replace")
    info = read_info(gcode, entries, plate)
    return SlicedFile(gcode=gcode, plate=plate, info=info, name=name, _entries=entries, _order=order)


def read_info(gcode: str, entries: dict[str, bytes], plate: int) -> PlateInfo:
    info = PlateInfo()
    header = _header_block(gcode)
    config = _config_block(gcode)

    info.slicer = header.get("_first", "")
    info.max_z_height = _num(header.get("max_z_height"))
    ids = [s.strip() for s in header.get("model label id", "").split(",") if s.strip()]
    heights = [_num(s) for s in header.get("object max height", "").split(",") if s.strip()]
    if ids and len(ids) == len(heights):
        info.object_heights = dict(zip(ids, heights, strict=True))
    m = re.search(
        r"total estimated time: (?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(\d+)s", header.get("_raw", "")
    )
    if m:
        d, h, mi, s = (int(x) if x else 0 for x in m.groups())
        info.print_seconds = ((d * 24 + h) * 60 + mi) * 60 + s

    info.printer_model = config.get("printer_model", "")
    info.bed_type = config.get("curr_bed_type", "")
    area = config.get("printable_area", "")
    pts = [p.split("x") for p in area.split(",") if "x" in p]
    if pts:
        info.bed_x = max(_num(p[0]) for p in pts)
        info.bed_y = max(_num(p[1]) for p in pts)
    info.printable_height = _num(config.get("printable_height"))
    chamber = [_num(c) for c in re.split(r"[;,]", config.get("chamber_temperatures", "")) if c.strip()]
    info.chamber_temp = max(chamber, default=0.0)
    cfg_filaments = [f.strip() for f in re.split(r"[;,]", config.get("filament_type", "")) if f.strip()]

    sinfo = entries.get("Metadata/slice_info.config")
    used: list[str] = []
    if sinfo:
        try:
            root = ET.fromstring(sinfo)
        except ET.ParseError:
            root = None
        for p in root.iter("plate") if root is not None else []:
            meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
            if str(meta.get("index", "1")) != str(plate):
                continue
            info.printer_model_id = meta.get("printer_model_id", "") or info.printer_model_id
            if not info.print_seconds and meta.get("prediction"):
                info.print_seconds = _num(meta["prediction"])
            for o in p.findall("object"):
                if o.get("identify_id"):
                    info.object_names[o.get("identify_id")] = o.get("name", "")
            for f in p.findall("filament"):
                if f.get("type"):
                    used.append(f.get("type"))
                info.filament_grams += _num(f.get("used_g"))
    plate_json = entries.get(f"Metadata/plate_{plate}.json")
    if plate_json and not info.bed_type:
        try:
            info.bed_type = str(json.loads(plate_json).get("bed_type", ""))
        except ValueError:
            pass
    info.filaments = sorted(set(used or cfg_filaments))
    return info


def _header_block(gcode: str) -> dict[str, str]:
    out: dict[str, str] = {}
    start = gcode.find("; HEADER_BLOCK_START")
    end = gcode.find("; HEADER_BLOCK_END")
    if start < 0 or end < 0:
        return out
    block = gcode[start:end]
    out["_raw"] = block
    lines = block.splitlines()[1:]
    if lines:
        out["_first"] = lines[0].lstrip("; ").strip()
    for line in lines:
        # "; total estimated time" shares a line with "model printing time"; keep the raw block for it.
        m = _HEADER_KV.match(line)
        if m:
            key = re.sub(r"\s*\[.*?\]\s*", "", m.group(1)).strip().lower()
            out.setdefault(key, m.group(2))
    return out


def _config_block(gcode: str) -> dict[str, str]:
    out: dict[str, str] = {}
    start = gcode.find("; CONFIG_BLOCK_START")
    end = gcode.find("; CONFIG_BLOCK_END")
    if start < 0 or end < 0:
        return out
    for line in gcode[start:end].splitlines():
        m = _CONFIG_KV.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Layout:
    """Line indexes of the structural markers inside one plate's G-code."""

    lines: list[str]
    exec_start: int
    exec_end: int
    start_gcode_end: int
    end_gcode_start: int

    @property
    def ok(self) -> bool:
        return min(self.exec_start, self.exec_end, self.start_gcode_end, self.end_gcode_start) >= 0 and (
            self.exec_start < self.start_gcode_end < self.end_gcode_start < self.exec_end
        )

    def missing(self) -> list[str]:
        names = {
            EXEC_START: self.exec_start,
            START_GCODE_END: self.start_gcode_end,
            END_GCODE_START: self.end_gcode_start,
            EXEC_END: self.exec_end,
        }
        return [k for k, v in names.items() if v < 0]


def layout(gcode: str) -> Layout:
    """Find the markers of the first loop in a plate (FarmLoop files repeat the whole plate per loop)."""
    lines = gcode.splitlines(keepends=True)

    def find(marker: str, after: int = 0) -> int:
        for i in range(max(0, after), len(lines)):
            if lines[i].rstrip("\r\n") == marker:
                return i
        return -1

    es = find(EXEC_START)
    sge = find(START_GCODE_END, es)
    egs = find(END_GCODE_START, sge)
    ee = find(EXEC_END, egs)
    return Layout(lines, es, ee, sge, egs)
