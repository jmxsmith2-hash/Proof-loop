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

from synthux.core.config import ConfigError

EXECUTABLES = {
    "bambu": ["bambu-studio", "BambuStudio", "bambu-studio.exe"],
    "orca": ["orca-slicer", "OrcaSlicer", "orca-slicer.exe"],
}


@dataclass
class Slicer:
    kind: str                 # bambu | orca
    exe: Path
    profile_dirs: list[Path] = field(default_factory=list)   # folders holding BBL/{machine,process,filament}

    @property
    def label(self) -> str:
        return {"bambu": "Bambu Studio", "orca": "OrcaSlicer"}[self.kind]


def _candidates() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    env = os.environ.get("SYNTHUX_SLICER")
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
    extra = os.environ.get("SYNTHUX_SLICER_PROFILES")
    if extra:
        dirs.insert(0, Path(extra))
    return [d.resolve() for d in dirs if (d / "BBL").is_dir()]


def find_slicer(explicit: str | Path | None = None) -> Slicer | None:
    pairs = [("orca" if "orca" in str(explicit).lower() else "bambu", Path(explicit))] if explicit else _candidates()
    for kind, exe in pairs:
        if exe.is_file():
            return Slicer(kind, exe, profile_dirs_for(kind, exe))
    if explicit:
        raise ConfigError(f"no slicer at {explicit}")
    return None


# ------------------------------------------------------------------------------- presets

class PresetLibrary:
    """System presets of one vendor (BBL), found by name, flattened with their parents and templates."""

    def __init__(self, dirs: list[Path], vendor: str = "BBL"):
        self.by_name: dict[str, dict[str, Any]] = {}
        self.kind_of: dict[str, str] = {}
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

    def flat(self, name: str) -> dict[str, Any]:
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
        return merged

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


def write_presets(lib: PresetLibrary, folder: Path, machine: str, process: str, filament: str,
                  overrides: dict[str, Any] | None = None) -> Presets:
    folder.mkdir(parents=True, exist_ok=True)
    files = {}
    overrides = overrides or {}
    for kind, name in (("machine", machine), ("process", process), ("filament", filament)):
        data = lib.flat(name)
        for key, value in overrides.get(kind, {}).items():
            current = data.get(key)
            data[key] = [str(value)] * len(current) if isinstance(current, list) else str(value)
        # Which printer / process / filament this is, as Bambu Studio and Orca record it in a project:
        # the CLI checks the process is compatible with the printer by these names.
        if kind == "machine":
            data["printer_settings_id"] = name
        elif kind == "process":
            data["print_settings_id"] = name
        else:
            data["filament_settings_id"] = [name]
        path = folder / f"{kind}.json"
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        files[kind] = path
    return Presets(machine, process, filament, files, overrides)


# ------------------------------------------------------------------------------- slicing

def slice_model(slicer: Slicer, model: Path, presets: Presets, out_dir: Path, *, arrange: bool = True,
                orient: bool = False, timeout: int = 900) -> tuple[Path, str]:
    """Run the slicer. Returns (the sliced .gcode.3mf, the slicer's log). Raises ConfigError on failure."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{Path(model).name.split('.')[0]}.gcode.3mf"
    cmd = [str(slicer.exe), "--slice", "0", "--export-3mf", str(out),
           "--load-settings", f"{presets.files['machine']};{presets.files['process']}",
           "--load-filaments", str(presets.files["filament"]),
           "--arrange", "1" if arrange else "0", "--orient", "1" if orient else "0",
           "--outputdir", str(out_dir), str(model)]
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and shutil.which("xvfb-run") \
            and not str(slicer.exe).endswith(".py"):
        cmd = ["xvfb-run", "-a", *cmd]                 # the slicer's toolkit wants a display even headless
    if str(slicer.exe).endswith(".py"):
        cmd = [sys.executable, *cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                              errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise ConfigError(f"{slicer.label} took longer than {timeout // 60} minutes") from exc
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or not out.is_file():
        tail = "\n".join(log.strip().splitlines()[-12:])
        raise ConfigError(f"{slicer.label} could not slice {Path(model).name} (exit {proc.returncode}):\n{tail}")
    return out, log
