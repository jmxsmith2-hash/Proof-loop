"""Slice a model headlessly with Bambu Studio or OrcaSlicer, using the printer's own presets.

Both slicers take the same command line (options as defined in Bambu Studio's PrintConfig.cpp):

    bambu-studio --slice 0 --export-3mf out.gcode.3mf
                 --load-settings "machine.json;process.json" --load-filaments "filament.json"
                 --arrange 1 --orient 0 model.stl

System presets inherit from one another by name ("Bambu Lab P2S 0.4 nozzle" -> "fdm_bbl_3dp_001_common"
-> "fdm_machine_common"), and a printer's start and end G-code come from companion presets named
"<printer> template <key>". The presets passed to the slicer are flattened here first, so each file
is complete on its own. Overrides (support on or off, ...) are applied to the flattened copies.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from proofloop.core.config import ConfigError
from proofloop.core.proc import NO_WINDOW

EXECUTABLES = {
    "bambu": ["bambu-studio", "BambuStudio", "bambu-studio.exe"],
    "orca": ["orca-slicer", "OrcaSlicer", "orca-slicer.exe"],
}


@dataclass
class Slicer:
    kind: str                 # bambu | orca
    exe: Path
    profile_dirs: list[Path] = field(default_factory=list)   # folders holding BBL/{machine,process,filament}
    user_dirs: list[Path] = field(default_factory=list)      # your own presets: {machine,process,filament}/*.json

    @property
    def label(self) -> str:
        return {"bambu": "Bambu Studio", "orca": "OrcaSlicer"}[self.kind]


def _candidates() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    env = os.environ.get("PROOFLOOP_SLICER")
    if env:
        out.append(("orca" if "orca" in env.lower() else "bambu", Path(env)))
    if sys.platform == "win32":
        for base in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                                  os.environ.get("LOCALAPPDATA"))):
            out += [("bambu", Path(base) / "Bambu Studio" / "bambu-studio.exe"),
                    ("orca", Path(base) / "OrcaSlicer" / "orca-slicer.exe")]
    elif sys.platform == "darwin":
        out += [("bambu", Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")),
                ("orca", Path("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"))]
    for kind, names in EXECUTABLES.items():
        for n in names:
            found = shutil.which(n)
            if found:
                out.append((kind, Path(found)))
    for folder in (Path.home() / "Applications", Path.home() / "Downloads", Path.home() / ".local/bin"):
        if folder.is_dir():
            for app in sorted(folder.glob("*.AppImage")):
                low = app.name.lower()
                if "bambu" in low or "orca" in low:
                    out.append(("orca" if "orca" in low else "bambu", app))
    return out


def profile_dirs_for(kind: str, exe: Path) -> list[Path]:
    """Where the slicer keeps its system presets: the updated copy in the user folder first."""
    app = {"bambu": "BambuStudio", "orca": "OrcaSlicer"}[kind]
    dirs: list[Path] = []
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        dirs.append(Path(os.environ["APPDATA"]) / app / "system")
    elif sys.platform == "darwin":
        dirs.append(Path.home() / "Library" / "Application Support" / app / "system")
    else:
        dirs.append(Path.home() / ".config" / app / "system")
    exe = Path(exe)
    dirs += [exe.parent / "resources" / "profiles", exe.parent.parent / "Resources" / "profiles",
             exe.parent / ".." / "resources" / "profiles"]
    extra = os.environ.get("PROOFLOOP_SLICER_PROFILES")
    if extra:
        dirs.insert(0, Path(extra))
    return [d.resolve() for d in dirs if (d / "BBL").is_dir()]


def user_dirs_for(kind: str) -> list[Path]:
    """Where the slicer keeps the presets you saved yourself: one folder per account (``user/<id>``)
    and ``user/default`` when signed out, each with ``machine``, ``process`` and ``filament`` in it."""
    app = {"bambu": "BambuStudio", "orca": "OrcaSlicer"}[kind]
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"]) / app / "user"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / app / "user"
    else:
        base = Path.home() / ".config" / app / "user"
    if not base.is_dir():
        return []
    return sorted(d for d in base.iterdir() if d.is_dir() and any((d / k).is_dir() for k in ("machine", "process", "filament")))


def find_slicer(explicit: str | Path | None = None) -> Slicer | None:
    pairs = [("orca" if "orca" in str(explicit).lower() else "bambu", Path(explicit))] if explicit else _candidates()
    for kind, exe in pairs:
        if exe.is_file():
            return Slicer(kind, exe, profile_dirs_for(kind, exe), user_dirs_for(kind))
    if explicit:
        raise ConfigError(f"no slicer at {explicit}")
    return None


# ------------------------------------------------------------------------------- presets

class PresetLibrary:
    """System presets of one vendor (BBL), found by name, flattened with their parents and templates."""

    def __init__(self, dirs: list[Path], vendor: str = "BBL", user_dirs: list[Path] | None = None):
        self.by_name: dict[str, dict[str, Any]] = {}
        self.kind_of: dict[str, str] = {}
        self.user: set[str] = set()  # presets you saved yourself in the slicer
        self._flat: dict[str, dict[str, Any]] = {}
        for d in reversed(dirs):                       # earlier folders win (the user's updated copy)
            root = Path(d) / vendor
            for kind in ("machine", "process", "filament"):
                for f in sorted((root / kind).glob("**/*.json")):
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if isinstance(data, dict) and data.get("name"):
                        self.by_name[data["name"]] = data
                        self.kind_of[data["name"]] = kind
        for d in user_dirs or []:
            for kind in ("machine", "process", "filament"):
                for f in sorted((Path(d) / kind).glob("*.json")):
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    name = data.get("name") if isinstance(data, dict) else None
                    if name and name not in self.by_name:  # never hides a system preset of the same name
                        self.by_name[name] = {**data, "instantiation": "true"}
                        self.kind_of[name] = kind
                        self.user.add(name)

    def flat(self, name: str) -> dict[str, Any]:
        if name in self._flat:
            return dict(self._flat[name])
        if name not in self.by_name:
            raise ConfigError(f"the slicer has no preset called {name!r}")
        chain, seen, cur = [], set(), name
        while cur and cur in self.by_name and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = self.by_name[cur].get("inherits") or ""
        merged: dict[str, Any] = {}
        for n in reversed(chain):                      # parents first, the preset itself last
            merged.update(self.by_name[n])
            prefix = f"{n} template "
            for tname, tdata in self.by_name.items():  # "<printer> template machine_start_gcode" etc.
                if tname.startswith(prefix):
                    key = tname[len(prefix):]
                    if key in tdata:
                        merged[key] = tdata[key]
        merged["name"] = name
        merged.pop("inherits", None)
        merged["from"] = "User"
        merged["instantiation"] = "true"
        self._flat[name] = merged
        return dict(merged)

    def system_base(self, name: str) -> str:
        """The system preset one of yours is based on (itself for a system preset)."""
        seen = set()
        while name in self.user and name not in seen:
            seen.add(name)
            name = self.by_name[name].get("inherits") or name
        return name

    def names(self, kind: str) -> list[str]:
        return [n for n, k in self.kind_of.items() if k == kind and self.by_name[n].get("instantiation") == "true"]

    def machine(self, printer_label: str, nozzle: float) -> str:
        want = f"{printer_label} {nozzle:g} nozzle"
        if want in self.by_name:
            return want
        options = [n for n in self.names("machine") if n.startswith(printer_label)]
        if not options:
            raise ConfigError(f"the slicer has no preset for {printer_label}; update its printer profiles")
        return sorted(options, key=lambda n: (f"{nozzle:g} nozzle" not in n, n))[0]

    def _compatible(self, name: str, machine: str) -> bool:
        flat = self.flat(name)
        comp = flat.get("compatible_printers") or []
        return machine in comp

    def process(self, machine: str, layer: float, wanted: str | None = None) -> str:
        if wanted:
            if wanted not in self.by_name:
                raise ConfigError(f"the slicer has no process preset called {wanted!r}")
            return wanted
        fits = [n for n in self.names("process") if self._compatible(n, machine)]
        if not fits:
            raise ConfigError(f"the slicer has no process preset for {machine}")

        def score(n: str) -> tuple:
            h = float(str(self.flat(n).get("layer_height", "0")).strip("[]'\" ") or 0)
            return (abs(h - layer) > 1e-6, "Standard" not in n, abs(h - layer), n)

        return sorted(fits, key=score)[0]

    def filament(self, machine: str, material: str, wanted: str | None = None) -> str:
        if wanted:
            if wanted not in self.by_name:
                raise ConfigError(f"the slicer has no filament preset called {wanted!r}")
            return wanted
        mat = material.upper()
        fits = []
        for n in self.names("filament"):
            flat = self.flat(n)
            types = flat.get("filament_type") or []
            types = types if isinstance(types, list) else [types]
            if mat in (t.upper() for t in types) and machine in (flat.get("compatible_printers") or []):
                fits.append(n)
        if not fits:
            raise ConfigError(f"the slicer has no {material} filament preset for {machine}")

        def score(n: str) -> tuple:  # Bambu's own Basic first, then Bambu, then generic
            return (not n.startswith(f"Bambu {material} Basic"), not n.startswith("Bambu "),
                    not n.startswith("Generic "), len(n), n)

        return sorted(fits, key=score)[0]


@dataclass
class Presets:
    machine: str
    process: str
    filament: str
    files: dict[str, Path]
    overrides: dict[str, Any]
    filament_files: list[Path] = field(default_factory=list)  # one per filament slot, in slot order


def write_presets(lib: PresetLibrary, folder: Path, machine: str, process: str, filament: str,
                  overrides: dict[str, Any] | None = None,
                  slots: list[tuple[str, str]] | None = None) -> Presets:
    """Write the presets to slice with. ``slots``: (filament preset, colour or "") for filament slots 1, 2...
    when the plate uses more than one; ``filament`` is then slot 1's."""
    folder.mkdir(parents=True, exist_ok=True)
    files = {}
    overrides = overrides or {}
    for kind, name in (("machine", machine), ("process", process), ("filament", filament)):
        data = lib.flat(name)
        for key, value in overrides.get(kind, {}).items():
            current = data.get(key)
            if isinstance(value, list):  # a whole list (points of an area): taken as it is
                data[key] = [str(v) for v in value]
            else:
                data[key] = [str(value)] * max(len(current), 1) if isinstance(current, list) else str(value)
        # Written the way Bambu Studio exports a user preset: "from": "User" plus "inherits" naming the
        # system preset. The CLI finds the system printer through "inherits" to check that the process
        # is compatible with it (without it: exit -17, process not compatible), and these ids name
        # them the way a project file does.
        data["from"], data["inherits"] = "User", lib.system_base(name)
        if kind == "machine":
            data["printer_settings_id"] = name
        elif kind == "process":
            data["print_settings_id"] = name
        else:
            data["filament_settings_id"] = [name]
        path = folder / f"{kind}.json"
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        files[kind] = path
    fil_files = [files["filament"]]
    if slots:
        fil_files = []
        for n, (name, colour) in enumerate(slots, 1):
            over = {**overrides.get("filament", {}), **({"filament_colour": colour} if colour else {})}
            one = write_presets(lib, folder / f"slot{n}", machine, process, name, {"filament": over})
            fil_files.append(one.files["filament"])
        files["filament"] = fil_files[0]
    return Presets(machine, process, filament, files, overrides, fil_files)


# ------------------------------------------------------------------------------- slicing

def _slicer_reason(out_dir: Path) -> str:
    """Bambu Studio and Orca write result.json next to their output, with the reason they stopped."""
    try:
        data = json.loads((Path(out_dir) / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("error_string") or data.get("message") or "").strip()


def slice_model(slicer: Slicer, model: Path | list[Path], presets: Presets | None, out_dir: Path, *,
                arrange: bool = True, orient: bool = False, copies: int | list[int] = 1,
                filament_ids: list[int] | None = None, timeout: int = 900) -> tuple[Path, str]:
    """Run the slicer. Returns (the sliced .gcode.3mf, the slicer's log). Raises ConfigError on failure.

    Without ``presets`` a project 3MF is sliced with its own settings. ``copies`` puts that many of
    the model on the plate (``--clone-objects``), arranged by the slicer. Several models go on one
    plate together, with ``copies`` and ``filament_ids`` (the filament slot each prints in) per model.
    """
    models = [Path(m) for m in (model if isinstance(model, list) else [model])]
    counts = copies if isinstance(copies, list) else [copies] * len(models)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{models[0].name.split('.')[0]}.gcode.3mf"
    # --export-3mf takes a file name: the slicers put it inside --outputdir themselves (an absolute
    # path there becomes <outputdir>/<absolute path> and the export fails with exit -13).
    cmd = [str(slicer.exe), "--slice", "0", "--export-3mf", out.name]
    if presets is not None:
        fils = presets.filament_files or [presets.files["filament"]]
        cmd += ["--load-settings", f"{presets.files['machine']};{presets.files['process']}",
                "--load-filaments", ";".join(map(str, fils))]
    if filament_ids and filament_ids != [1] * len(models):
        cmd += ["--load-filament-ids", ",".join(map(str, filament_ids))]
        if len(set(filament_ids)) > 1:  # else arranging puts each colour on a plate of its own
            cmd += ["--allow-multicolor-oneplate"]
    if any(c > 1 for c in counts):
        cmd += ["--clone-objects", ",".join(map(str, counts))]  # "--repetitions" only works when slicing one plate
    cmd += ["--arrange", "1" if arrange else "0", "--orient", "1" if orient else "0",
            "--outputdir", str(out_dir), *map(str, models)]
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and shutil.which("xvfb-run") \
            and not str(slicer.exe).endswith(".py"):
        cmd = ["xvfb-run", "-a", *cmd]                 # the slicer's toolkit wants a display even headless
    if str(slicer.exe).endswith(".py"):
        cmd = [sys.executable, *cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                              errors="replace", creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise ConfigError(f"{slicer.label} took longer than {timeout // 60} minutes") from exc
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or not out.is_file():
        tail = "\n".join(log.strip().splitlines()[-12:])
        code = proc.returncode - (1 << 32) if proc.returncode > (1 << 31) else proc.returncode  # Windows: unsigned
        reason = _slicer_reason(out_dir)
        raise ConfigError(f"{slicer.label} could not slice {', '.join(m.name for m in models)} (exit {code})"
                          + (f": {reason}" if reason else "") + (f"\n{tail}" if tail else ""))
    return out, log
