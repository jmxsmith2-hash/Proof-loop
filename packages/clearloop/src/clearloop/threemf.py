"""Read and write sliced plate files: Bambu/Orca ``.gcode.3mf`` and plain ``.gcode``."""

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
_COMMENT_KV = re.compile(r"^;\s*([a-z_][a-z_ \[\]]*?)\s*=\s*(.+)$", re.M)


@dataclass
class PlateInfo:
    filaments: list[str] = field(default_factory=list)
    filament_grams: float = 0.0
    print_seconds: float = 0.0
    chamber_temp: float = 0.0
    printer_model: str = ""


@dataclass
class SlicedFile:
    """One plate's G-code plus everything needed to write the container back."""

    gcode: str
    plate: int
    info: PlateInfo
    container: str  # "3mf" or "gcode"
    _entries: dict[str, bytes] = field(default_factory=dict, repr=False)
    _order: list[str] = field(default_factory=list, repr=False)

    @property
    def gcode_name(self) -> str:
        return f"Metadata/plate_{self.plate}.gcode"

    def to_bytes(self, gcode: str, extra: dict[str, bytes] | None = None) -> bytes:
        data = gcode.encode()
        if self.container == "gcode":
            return data
        entries = dict(self._entries)
        entries[self.gcode_name] = data
        # Bambu firmware checks this checksum before it will start the plate.
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
    raw = path.read_bytes()
    return loads(raw, name=path.name, plate=plate)


def loads(raw: bytes, name: str = "", plate: int | None = None) -> SlicedFile:
    if raw[:2] == b"PK":
        return _load_3mf(raw, plate)
    gcode = raw.decode("utf-8", errors="replace")
    return SlicedFile(gcode=gcode, plate=plate or 1, info=_info_from_gcode(gcode), container="gcode")


def _load_3mf(raw: bytes, plate: int | None) -> SlicedFile:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        order = z.namelist()
        entries = {n: z.read(n) for n in order}
    plates = sorted(int(m.group(1)) for n in order if (m := _PLATE_RE.match(n)))
    if not plates:
        raise ValueError("no sliced G-code in this 3MF; slice the plate and export it as .gcode.3mf")
    if plate is None:
        plate = plates[0]
    elif plate not in plates:
        raise ValueError(f"plate {plate} not sliced; available: {plates}")
    gcode = entries[f"Metadata/plate_{plate}.gcode"].decode("utf-8", errors="replace")
    info = _info_from_3mf(entries, plate)
    if not info.filaments:
        info.filaments = _info_from_gcode(gcode).filaments
    return SlicedFile(gcode=gcode, plate=plate, info=info, container="3mf", _entries=entries, _order=order)


def _info_from_3mf(entries: dict[str, bytes], plate: int) -> PlateInfo:
    info = PlateInfo()
    settings = {}
    if "Metadata/project_settings.config" in entries:
        try:
            settings = json.loads(entries["Metadata/project_settings.config"])
        except ValueError:
            settings = {}
    info.printer_model = str(settings.get("printer_model", ""))
    chamber = settings.get("chamber_temperatures") or settings.get("chamber_temperature") or []
    if isinstance(chamber, str):
        chamber = [chamber]
    info.chamber_temp = max((float(c) for c in chamber if _is_number(c)), default=0.0)

    used_types: list[str] = []
    if "Metadata/slice_info.config" in entries:
        try:
            root = ET.fromstring(entries["Metadata/slice_info.config"])
        except ET.ParseError:
            root = None
        for p in root.iter("plate") if root is not None else []:
            meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
            if str(meta.get("index", "1")) != str(plate):
                continue
            if _is_number(meta.get("prediction")):
                info.print_seconds = float(meta["prediction"])
            for f in p.findall("filament"):
                if f.get("type"):
                    used_types.append(f.get("type"))
                if _is_number(f.get("used_g")):
                    info.filament_grams += float(f.get("used_g"))
    if not used_types:
        types = settings.get("filament_type") or []
        used_types = [t for t in types if t]
    info.filaments = sorted(set(used_types))
    return info


def _info_from_gcode(gcode: str) -> PlateInfo:
    info = PlateInfo()
    # Slicer settings live in comments at the head or tail of the file.
    sample = gcode[:200_000] + gcode[-400_000:]
    for key, value in _COMMENT_KV.findall(sample):
        if key == "filament_type":
            info.filaments = sorted({v.strip() for v in re.split(r"[;,]", value) if v.strip()})
        elif key in ("chamber_temperature", "chamber_temperatures"):
            nums = [float(v) for v in re.split(r"[;,]", value) if _is_number(v.strip())]
            info.chamber_temp = max(nums, default=0.0)
        elif key in ("total filament weight [g]", "filament used [g]"):
            nums = [float(v) for v in re.split(r"[;,]", value) if _is_number(v.strip())]
            info.filament_grams = sum(nums)
    m = re.search(r"; total estimated time: (?:(\d+)h )?(?:(\d+)m )?(\d+)s", sample)
    if m:
        h, mi, s = (int(x) if x else 0 for x in m.groups())
        info.print_seconds = h * 3600 + mi * 60 + s
    return info


def _is_number(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False
