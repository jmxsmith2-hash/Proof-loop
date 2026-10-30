"""Load CAD exports into a single triangle mesh in millimetres."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from proofloop.core.config import ConfigError, load_yaml_file
from proofloop.core.logging import get_logger
from proofloop.core.paths import config_file

log = get_logger(__name__)

MESH_FORMATS = {".stl", ".obj", ".3mf", ".ply", ".glb", ".gltf", ".off"}
STEP_FORMATS = {".step", ".stp"}
CODE_FORMATS = {".scad"}  # parts written as code, rendered with OpenSCAD
OPENSCAD_PLACES = ("C:/Program Files/OpenSCAD/openscad.exe", "C:/Program Files (x86)/OpenSCAD/openscad.exe",
                   "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD", "/usr/bin/openscad", "/usr/local/bin/openscad",
                   "/opt/homebrew/bin/openscad")
UNIT_SCALE = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4}


@dataclass
class Model:
    path: Path
    mesh: trimesh.Trimesh
    file_hash: str
    units_note: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def size(self) -> np.ndarray:
        return self.mesh.extents

    def shells(self) -> list[trimesh.Trimesh]:
        """Separate bodies (computed once; splitting a dense mesh is slow)."""
        if "_shells" not in self.mesh.metadata:
            # repair=False: trimesh would try to fill holes in each open body, which needs an extra package
            # (networkx) and changes the geometry being checked
            parts = self.mesh.split(only_watertight=False, repair=False)
            self.mesh.metadata["_shells"] = [s for s in parts if len(s.faces) >= 4]
        return self.mesh.metadata["_shells"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def openscad() -> str | None:
    """OpenSCAD on this computer (``PROOFLOOP_OPENSCAD`` points to it if it's somewhere else)."""
    import os
    import shutil

    given = os.environ.get("PROOFLOOP_OPENSCAD")
    if given:
        return given
    return shutil.which("openscad") or next((p for p in OPENSCAD_PLACES if Path(p).is_file()), None)


def _render_scad(path: Path) -> trimesh.Trimesh:
    """Render an OpenSCAD file to a mesh, with OpenSCAD itself (its own preview of the part is what's checked)."""
    import subprocess
    import sys
    import tempfile

    exe = openscad()
    if not exe:
        raise ConfigError(f"{path.name}: checking OpenSCAD files needs OpenSCAD (openscad.org), or export STL from it")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / (path.stem + ".stl")
        cmd = [exe, "-o", str(out), str(path)]
        if exe.endswith(".py"):  # a stand-in OpenSCAD, in tests
            cmd = [sys.executable, *cmd]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigError(f"OpenSCAD couldn't render {path.name}: {exc}") from exc
        if r.returncode or not out.is_file():
            err = r.stderr.decode("utf-8", "replace").strip().splitlines()
            raise ConfigError(f"OpenSCAD couldn't render {path.name}: " + (err[-1] if err else f"exit {r.returncode}"))
        return _read(out)


def _read(path: Path) -> trimesh.Trimesh:
    suffix = path.suffix.lower()
    if suffix in CODE_FORMATS:
        return _render_scad(path)
    if suffix in STEP_FORMATS:
        try:
            import cascadio  # noqa: F401  (optional: pip install proofloop[step])
        except ImportError as exc:
            raise ConfigError(f"{path.name}: reading STEP needs `pip install proofloop[step]`, "
                              "or export STL/3MF/OBJ from your CAD tool") from exc
    elif suffix not in MESH_FORMATS:
        raise ConfigError(f"unsupported CAD file {path.name}; use STL, 3MF, OBJ, PLY, GLB, STEP or OpenSCAD")
    if suffix == ".3mf":
        from proofloop.check.cad.threemf import read_3mf

        return read_3mf(path)
    loaded = trimesh.load(str(path), force="mesh", process=True)
    if isinstance(loaded, trimesh.Scene):  # pragma: no cover (force="mesh" normally merges)
        loaded = loaded.dump(concatenate=True)
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise ConfigError(f"{path.name} contains no triangles")
    return loaded


def guess_units(extents: np.ndarray) -> tuple[float, str]:
    """Most CAD exports are mm. A part under 1 unit across is almost certainly metres."""
    biggest = float(np.max(extents))
    if biggest < 1.0:
        return 1000.0, f"largest dimension is {biggest:g}; assumed metres and scaled x1000 (override with --units)"
    if biggest < 15.0:
        return 1.0, (f"largest dimension is only {biggest:g} mm; if this was exported in inches or cm, "
                     "re-run with --units in or --units cm")
    return 1.0, ""


def load_model(path: Path | str, units: str | None = None) -> Model:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"CAD file not found: {path}")
    mesh = _read(path)
    if units:
        if units not in UNIT_SCALE:
            raise ConfigError(f"unknown units {units!r}; use one of {sorted(UNIT_SCALE)}")
        scale, note = UNIT_SCALE[units], f"units given as {units}"
    else:
        scale, note = guess_units(mesh.extents)
    if scale != 1.0:
        mesh.apply_scale(scale)
    model = Model(path=path, mesh=mesh, file_hash=_sha(path), units_note=note)
    log.info("loaded %s: %d triangles, %.1f x %.1f x %.1f mm", path.name, len(mesh.faces), *mesh.extents)
    return model


def load_profiles(config_dir: Path, printer: str | None, material: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    data = load_yaml_file(config_file(config_dir, "printers.yaml"))
    if not data:
        raise ConfigError(f"missing {Path(config_dir) / 'printers.yaml'}")
    if not printer:
        from proofloop.check.my_printers import default_printer

        printer = default_printer(data)  # your first printer under Printers, else printers.yaml's default
    material = material or data.get("default_material")
    # Slicer names ("PLA Basic", "PETG HF") and any capitalisation map to the profile
    wanted = str(material).strip().lower()
    for name, spec in data["materials"].items():
        if wanted == name.lower() or wanted in (a.lower() for a in spec.get("aliases", [])):
            material = name
            break
    if printer not in data["printers"]:
        raise ConfigError(f"unknown printer {printer!r}; known: {sorted(data['printers'])}")
    if material not in data["materials"]:
        raise ConfigError(f"unknown material {material!r}; known: {sorted(data['materials'])}")
    return ({"name": printer, **data["printers"][printer]}, {"name": material, **data["materials"][material]},
            data.get("slicer", {}))
