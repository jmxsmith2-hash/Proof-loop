"""Slice a model in ProofLoop, so a part goes from its file to a printer without opening the slicer.

ProofLoop drives the Bambu Studio (or OrcaSlicer) installed on this computer through its command
line (see ``proofloop.check.slicer``), with its own presets and the ones you saved in it:

* A model (``.stl``, ``.obj``, ``.step``) is sliced with the printer, process and filament presets
  you pick. The nozzle size comes from the printer preset and the nozzle flow (Standard or High
  Flow) from your printer, so the file matches what is fitted.
* Several models can share a plate, each with its own number of copies and its own filament. A
  filament picked from an AMS tray keeps that tray's number as its slot (tray A3 is slot 3), so
  plates sliced here from one printer's AMS can be batched into one run.
* A Bambu Studio project (``.3mf`` with the parts in it) is sliced with the project's own settings:
  painting, supports and modifiers stay as you made them. Only the nozzle flow changes, when it
  isn't the one your printer has; this is how a plate sliced for the wrong nozzle gets fixed.

The result is a normal sliced plate, ready for *Make a loop file*.
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proofloop.check.slicer import PresetLibrary, Slicer, find_slicer, slice_model, write_presets
from proofloop.core import printers
from proofloop.core.config import ConfigError
from proofloop.loopfiles import threemf

MESH_SUFFIXES = (".stl", ".obj", ".step", ".stp")
FLOWS = ("Standard", "High Flow")
PLATES = ("Textured PEI Plate", "Cool Plate", "Engineering Plate", "High Temp Plate", "Supertack Plate")
MAX_COPIES = 100
MAX_SLOT = 16  # four AMS units of four trays
FILL_COPIES = 100  # "fill the plate" asks for this many and keeps what fits on the first plate


class SliceError(ValueError):
    """The model can't be sliced as asked. The message says why, in words for the window."""


def kind_of(name: str, data: bytes) -> str:
    """``plate`` (already sliced), ``project`` (a slicer project with its parts), ``mesh`` or ``""``."""
    low = name.lower()
    if low.endswith(MESH_SUFFIXES):
        return "mesh"
    if not low.endswith(".3mf"):
        return ""
    if threemf.sliced_plates(data):
        return "plate"
    try:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    except zipfile.BadZipFile:
        return ""
    return "project" if any(n.endswith(".model") for n in names) else ""


@dataclass
class Choice:
    """What to slice with. Empty fields fall back to sensible defaults for the printer."""

    machine: str = ""        # printer preset, e.g. "Bambu Lab P2S 0.4 nozzle"
    process: str = ""        # e.g. "0.20mm Standard @BBL P2S"
    filament: str = ""       # e.g. "Bambu PLA Basic @BBL P2S"
    nozzle_flow: str = ""    # "Standard" or "High Flow"; "" keeps the preset's or project's own
    plate: str = ""          # build plate type, e.g. "Textured PEI Plate" (sets the bed temperature); "": the slicer's
    copies: int = 1
    fill: bool = False       # as many copies as fit on one plate (``copies`` is then ignored)
    support: bool | None = None  # None: as the process preset has it
    orient: bool = False     # let the slicer turn the part to its best side
    clear_behind_y: float = 0.0  # keep parts in front of this Y (the sweep's drop-down strip); 0: anywhere
    filament_colour: str = ""  # the AMS tray's colour, so a send feeds it from that tray; "": the preset's
    filament_slot: int = 0   # the AMS tray's number + 1 (A1 = 1 ... D4 = 16); 0: the first free slot

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Choice:
        c = cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d and d[k] is not None})
        c.copies = int(c.copies or 1)
        if not 1 <= c.copies <= MAX_COPIES:
            raise SliceError(f"copies must be between 1 and {MAX_COPIES}")
        if c.nozzle_flow and c.nozzle_flow not in FLOWS:
            raise SliceError(f"nozzle flow must be one of {', '.join(FLOWS)}")
        if c.plate and c.plate not in PLATES:
            raise SliceError(f"the plate must be one of {', '.join(PLATES)}")
        c.filament_colour, c.filament_slot = _colour(c.filament_colour), _slot(c.filament_slot)
        return c


@dataclass
class Extra:
    """Another model on the same plate: how many of it, and the filament it prints in."""

    data: bytes
    name: str
    copies: int = 1
    filament: str = ""       # "": the first model's
    colour: str = ""
    slot: int = 0

    def __post_init__(self) -> None:
        self.copies = int(self.copies or 1)
        if not 1 <= self.copies <= MAX_COPIES:
            raise SliceError(f"copies of {self.name} must be between 1 and {MAX_COPIES}")
        self.colour, self.slot = _colour(self.colour), _slot(self.slot)


def _colour(v: Any) -> str:
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?", str(v or "").strip())
    return f"#{m.group(1).upper()}" if m else ""


def _slot(v: Any) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return n if 1 <= n <= MAX_SLOT else 0


def assign_slots(items: list[Extra]) -> tuple[list[tuple[str, str]], list[int]]:
    """The filament slots a plate's models print in: (preset, colour) for slots 1..n, and each model's slot.
    A model given a tray keeps that tray's slot; the others share a slot when their filament is the same,
    else take the first free one. Slots nothing prints in are filled with the first model's filament."""
    keys = [(it.filament, it.colour) for it in items]
    taken: dict[int, tuple[str, str]] = {}
    ids = [0] * len(items)
    for i, it in enumerate(items):
        if it.slot:
            if taken.get(it.slot, keys[i]) != keys[i]:
                raise SliceError(f"slot {it.slot} can't print two filaments: pick one for {it.name}")
            taken[it.slot], ids[i] = keys[i], it.slot
    for i in range(len(items)):
        if not ids[i]:
            ids[i] = next((s for s, k in taken.items() if k == keys[i]), 0) \
                or next(n for n in range(1, len(taken) + 2) if n not in taken)
            taken[ids[i]] = keys[i]
    filler = (keys[0][0], "")
    return [taken.get(n, filler) for n in range(1, max(taken) + 1)], ids


def mixed_name(names: list[str]) -> str:
    """The sliced plate's name: the first model's, and how many others share its plate."""
    first = sliced_name(names[0])[: -len(".gcode.3mf")]
    others = [sliced_name(n)[: -len(".gcode.3mf")] for n in names[1:]]
    if not others:
        return f"{first}.gcode.3mf"
    tail = others[0] if len(others) == 1 else f"{len(others)}_more"
    return f"{first}_and_{tail}.gcode.3mf"


# ------------------------------------------------------------------------------- presets

_LIB: dict[str, Any] = {"at": 0.0, "key": None, "lib": None}
_LIB_LOCK = threading.Lock()


def slicer_and_presets() -> tuple[Slicer, PresetLibrary]:
    """The slicer on this computer and its presets (read again at most once a minute)."""
    found = find_slicer()
    if found is None:
        raise SliceError("no Bambu Studio or OrcaSlicer found on this computer: install Bambu Studio to slice here")
    if not found.profile_dirs:
        raise SliceError(f"found {found.label}, but not its printer presets: open it once so it downloads them")
    key = (str(found.exe), tuple(map(str, found.profile_dirs)), tuple(map(str, found.user_dirs)))
    with _LIB_LOCK:
        if _LIB["key"] != key or time.monotonic() - _LIB["at"] > 60:
            _LIB.update(key=key, at=time.monotonic(), lib=PresetLibrary(found.profile_dirs, user_dirs=found.user_dirs))
        return found, _LIB["lib"]


def _first(v: Any) -> str:
    return str(v[0] if isinstance(v, list) and v else v or "")


def options(lib: PresetLibrary, model_id: str = printers.P2S) -> dict[str, Any]:
    """The presets that fit a printer model: its printer presets, and for each the processes and filaments."""
    label = f"Bambu Lab {printers.model_name(model_id)}"
    machines = [n for n in lib.names("machine") if (lib.flat(n).get("printer_model") or "") == label]
    machines.sort(key=lambda n: (n in lib.user, n))
    out: dict[str, Any] = {"printer_model": label, "machines": [], "processes": {}, "filaments": {}}
    for m in machines:
        flat = lib.flat(m)
        base = lib.system_base(m)
        out["machines"].append({"name": m, "nozzle": _first(flat.get("nozzle_diameter")), "mine": m in lib.user,
                                "flows": _flows(flat), "default_process": _first(flat.get("default_print_profile")),
                                "default_filament": _first(flat.get("default_filament_profile"))})
        procs = [n for n in lib.names("process") if base in (lib.flat(n).get("compatible_printers") or [])]
        out["processes"][m] = sorted(procs, key=lambda n: (n in lib.user, _layer(lib, n), n))
        fils = [n for n in lib.names("filament") if base in (lib.flat(n).get("compatible_printers") or [])]
        out["filaments"][m] = sorted(({"name": n, "type": _first(lib.flat(n).get("filament_type")),
                                       "id": _first(lib.flat(n).get("filament_id")), "mine": n in lib.user}
                                      for n in fils), key=lambda f: (not f["mine"], f["type"], f["name"]))
    return out


def _flows(flat: dict[str, Any]) -> list[str]:
    variants = ",".join(flat.get("printer_extruder_variant") or flat.get("extruder_variant_list") or [])
    return [f for f in FLOWS if f in variants] or ["Standard"]


def _bed_size(flat: dict[str, Any]) -> tuple[float, float]:
    """Width and depth of the bed from a printer preset's ``printable_area`` ("0x0", "256x0", ...)."""
    pts = [tuple(float(v) for v in str(p).split("x")) for p in flat.get("printable_area") or [] if "x" in str(p)]
    return (max(p[0] for p in pts), max(p[1] for p in pts)) if pts else (256.0, 256.0)


def _layer(lib: PresetLibrary, name: str) -> float:
    try:
        return float(_first(lib.flat(name).get("layer_height")) or 0)
    except ValueError:
        return 0.0


def printer_facts(name: str, state: Any, opts: dict[str, Any], lib: PresetLibrary) -> dict[str, Any]:
    """A printer as slicing needs it: its nozzle, the system preset for that nozzle, and the filament
    preset for each loaded AMS tray (from the printer's last report)."""
    size = float(getattr(state, "nozzle_diameter", 0) or 0)
    machine = next((m["name"] for m in opts["machines"] if size and m["nozzle"] and abs(float(m["nozzle"]) - size) < 1e-3
                    and not m["mine"]), "")
    trays = [{**t, "preset": filament_for_tray(lib, machine, t, opts) if machine else ""}
             for t in state.ams.trays() if not t.get("empty")]
    return {"name": name, "online": state.online, "nozzle": size or None, "nozzle_flow": state.nozzle_flow,
            "machine": machine, "trays": trays}


def usual_choice(usual: dict[str, Any], printer: dict[str, Any] | None, opts: dict[str, Any]) -> Choice:
    """What to slice with when nobody is there to pick: the presets you last used, made to fit the
    printer (its nozzle size and flow, and what's in its AMS when your filament doesn't fit)."""
    machines = {m["name"]: m for m in opts["machines"]}
    machine = usual.get("machine") or ""
    if printer and printer["machine"]:
        want = machines[printer["machine"]]["nozzle"]
        if machine not in machines or machines[machine]["nozzle"] != want:
            machine = printer["machine"]
    if machine not in machines:
        machine = next((m for m in machines if m.endswith(" 0.4 nozzle")), next(iter(machines), ""))
    process = usual.get("process") or ""
    if process not in opts["processes"].get(machine, []):
        process = ""
    fits = {f["name"] for f in opts["filaments"].get(machine, [])}
    filament = usual.get("filament") or ""
    if filament not in fits:
        loaded = [t["preset"] for t in (printer or {}).get("trays", []) if t.get("preset") in fits]
        filament = loaded[0] if loaded else ""
    flow = (printer or {}).get("nozzle_flow") or ""
    return Choice(machine=machine, process=process, filament=filament,
                  nozzle_flow=flow if flow in machines.get(machine, {}).get("flows", []) else "",
                  plate=usual.get("plate") or "", support=usual.get("support"), orient=bool(usual.get("orient")),
                  copies=int(usual.get("copies") or 1), fill=bool(usual.get("fill")))


def filament_for_tray(lib: PresetLibrary, machine: str, tray: dict[str, Any], opts: dict[str, Any]) -> str:
    """The filament preset for what an AMS tray holds: the one its RFID tag names, else one of the same type."""
    fils = opts["filaments"].get(machine, [])
    idx, ftype = str(tray.get("filament_id") or ""), str(tray.get("type") or "").upper()
    exact = [f["name"] for f in fils if idx and f["id"] == idx]
    if exact:
        return sorted(exact, key=lambda n: (n in lib.user, len(n)))[0]
    same = [f["name"] for f in fils if f["type"].upper() == ftype]
    if not same:
        return ""
    return sorted(same, key=lambda n: (not n.startswith("Bambu "), not n.startswith("Generic "), n in lib.user, len(n)))[0]


# ------------------------------------------------------------------------------- slicing

def sliced_name(name: str) -> str:
    stem = re.sub(r"(\.gcode)?\.(3mf|stl|obj|step|stp)$", "", Path(name).name, flags=re.IGNORECASE) or "model"
    return f"{stem}.gcode.3mf"


def as_project(data: bytes, nozzle_flow: str = "") -> bytes:
    """A project 3MF ready to slice: any old slice left out, and the nozzle flow set if asked."""
    zin = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for n in zin.namelist():
            if re.match(r"Metadata/plate_\d+\.gcode(\.md5)?$", n):
                continue
            body = zin.read(n)
            if n == "Metadata/project_settings.config" and nozzle_flow:
                cfg = json.loads(body.decode("utf-8"))
                for key in ("nozzle_volume_type", "default_nozzle_volume_type"):
                    if key in cfg or key == "nozzle_volume_type":
                        old = cfg.get(key)
                        cfg[key] = [nozzle_flow] * len(old) if isinstance(old, list) and old else [nozzle_flow]
                body = json.dumps(cfg, indent=4).encode("utf-8")
            out.writestr(n, body)
    return buf.getvalue()


def project_flow(data: bytes) -> str:
    """The nozzle flow a project is set up for, or ``""``."""
    try:
        cfg = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("Metadata/project_settings.config"))
    except (KeyError, ValueError, zipfile.BadZipFile):
        return ""
    return _first(cfg.get("nozzle_volume_type") or cfg.get("default_nozzle_volume_type"))


def slice_upload(data: bytes, name: str, choice: Choice, work: Path, *, model_id: str = printers.P2S,
                 found: Slicer | None = None, lib: PresetLibrary | None = None,
                 extra: list[Extra] | None = None) -> tuple[bytes, str, list[str]]:
    """Slice an uploaded model or project, with ``extra`` models on the same plate. Returns (the sliced
    .gcode.3mf, its name, notes for the window)."""
    kind = kind_of(name, data)
    extra = extra or []
    if kind == "plate":
        raise SliceError(f"{name} is already sliced: open it in Make a loop file as it is")
    if not kind:
        raise SliceError(f"{name} isn't a model ProofLoop can slice: use .stl, .obj, .step or a Bambu Studio .3mf project")
    if extra and kind == "project":
        raise SliceError(f"{name} is a Bambu Studio project, with its own parts: add models to it in Bambu Studio")
    for x in extra:
        if kind_of(x.name, x.data) != "mesh":
            raise SliceError(f"{x.name} isn't a model that can share the plate: use .stl, .obj or .step")
    if extra and choice.fill:
        raise SliceError("Fill the plate works with one model: say how many of each instead")
    if found is None or lib is None:
        found, lib = slicer_and_presets()
    work.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "model"
    if kind == "project":
        was = project_flow(data)
        if choice.nozzle_flow and was and was != choice.nozzle_flow:
            notes.append(f"sliced for a {choice.nozzle_flow} nozzle (the project was set for {was})")
        src = work / (safe if safe.lower().endswith(".3mf") else safe + ".3mf")
        src.write_bytes(as_project(data, choice.nozzle_flow))
        try:
            out, _ = slice_model(found, src, None, work / "out", arrange=False, orient=False)
        except ConfigError as e:
            raise SliceError(str(e)) from e
        return out.read_bytes(), sliced_name(name), notes
    opts = options(lib, model_id)
    machines = [m["name"] for m in opts["machines"]]
    machine = choice.machine or next((m for m in machines if m.endswith(" 0.4 nozzle")), machines[0] if machines else "")
    if machine not in machines:
        raise SliceError(f"{machine or 'no printer preset'} isn't a {opts['printer_model']} preset in {found.label}")
    m = next(x for x in opts["machines"] if x["name"] == machine)
    process = choice.process or m["default_process"]
    if process not in opts["processes"][machine]:
        raise SliceError(f"the process {process!r} doesn't fit {machine}")
    filament = choice.filament or m["default_filament"]
    fits = {f["name"] for f in opts["filaments"][machine]}
    items = [Extra(data, name, choice.copies, filament, choice.filament_colour, choice.filament_slot)]
    for x in extra:
        items.append(Extra(x.data, x.name, x.copies, x.filament or filament, x.colour, x.slot))
    for it in items:
        if it.filament not in fits:
            raise SliceError(f"the filament {it.filament!r} doesn't fit {machine}")
    if not choice.fill and sum(it.copies for it in items) > MAX_COPIES:
        raise SliceError(f"that's more than {MAX_COPIES} parts on one plate")
    slots, ids = assign_slots(items)
    over: dict[str, dict[str, Any]] = {}
    if choice.nozzle_flow:
        if choice.nozzle_flow not in m["flows"]:
            raise SliceError(f"{machine} has no {choice.nozzle_flow} nozzle")
        over["machine"] = {"nozzle_volume_type": choice.nozzle_flow, "default_nozzle_volume_type": choice.nozzle_flow}
    if choice.support is not None:
        over.setdefault("process", {})["enable_support"] = 1 if choice.support else 0
    if choice.plate:
        over.setdefault("process", {})["curr_bed_type"] = choice.plate
    if choice.clear_behind_y:
        # Arranging never places parts in an excluded area: the strip behind the parts where the
        # pusher comes down stays empty, so every copy can be swept.
        w, h = _bed_size(lib.flat(machine))
        if 0 < choice.clear_behind_y < h:
            y = f"{choice.clear_behind_y:g}"
            over.setdefault("machine", {})["bed_exclude_area"] = [f"0x{y}", f"{w:g}x{y}", f"{w:g}x{h:g}", f"0x{h:g}"]
    if len(set(ids)) > 1:
        # A prime tower would stay on the plate where the sweep can't clear it, and the next loop would
        # print onto it: the printer flushes into its chute instead.
        over.setdefault("process", {})["enable_prime_tower"] = 0
        notes.append(f"{len(set(ids))} filaments: each layer changes filament between them, which takes longer, "
                     "and no prime tower is printed, so the sweep leaves the plate clear")
    srcs = []
    for i, it in enumerate(items):
        one = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(it.name).name).strip("._") or "model"
        src = work / "models" / str(i) / one  # a folder each: two models may have the same name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(it.data)
        srcs.append(src)
    presets = write_presets(lib, work / "presets", machine, process, slots[0][0], over,
                            slots=slots if len(slots) > 1 or slots[0][1] else None)
    copies = [FILL_COPIES] if choice.fill else [it.copies for it in items]
    try:
        out, _ = slice_model(found, srcs, presets, work / "out", arrange=True, orient=choice.orient, copies=copies,
                             filament_ids=ids)
    except ConfigError as e:
        raise SliceError(str(e)) from e
    sliced = out.read_bytes()
    if extra:
        if len(threemf.sliced_plates(sliced)) > 1:
            notes.append("they don't all fit on one plate: the slicer made "
                         f"{len(threemf.sliced_plates(sliced))} plates")
        return sliced, mixed_name([it.name for it in items]), notes
    plates = threemf.sliced_plates(sliced)
    if choice.fill:
        # The slicer puts what doesn't fit on more plates: the first one is the plate full of copies.
        first = threemf.only_plate(sliced, plates[0]) if len(plates) > 1 else sliced
        made = len(threemf.loads(first, name=sliced_name(name)).info.object_names)
        notes.append(f"{made} copies fill the plate" + (" (the most ProofLoop tries)" if made >= FILL_COPIES else ""))
        return first, sliced_name(name), notes
    if len(plates) > 1:
        notes.append(f"{choice.copies} copies don't fit on one plate: the slicer made {len(plates)} plates")
    return sliced, sliced_name(name), notes
