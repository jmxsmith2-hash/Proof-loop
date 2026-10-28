"""Load CAD exports into a single triangle mesh in millimetres."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from synthux.core.config import ConfigError, load_yaml_file
from synthux.core.logging import get_logger

log = get_logger(__name__)

MESH_FORMATS = {".stl", ".obj", ".3mf", ".ply", ".glb", ".gltf", ".off"}
STEP_FORMATS = {".step", ".stp"}
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
            self.mesh.metadata["_shells"] = [s for s in self.mesh.split(only_watertight=False) if len(s.faces) >= 4]
        return self.mesh.metadata["_shells"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _read(path: Path) -> trimesh.Trimesh:
    suffix = path.suffix.lower()
    if suffix in STEP_FORMATS:
        try:
            import cascadio  # noqa: F401  (optional: pip install synthux[step])
        except ImportError as exc:
            raise ConfigError(f"{path.name}: reading STEP needs `pip install synthux[step]`, "
                              "or export STL/3MF/OBJ from your CAD tool") from exc
    elif suffix not in MESH_FORMATS:
        raise ConfigError(f"unsupported CAD file {path.name}; use STL, 3MF, OBJ, PLY, GLB or STEP")
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
    data = load_yaml_file(Path(config_dir) / "printers.yaml")
    if not data:
        raise ConfigError(f"missing {Path(config_dir) / 'printers.yaml'}")
    printer = printer or data.get("default_printer")
    material = material or data.get("default_material")
    if printer not in data["printers"]:
        raise ConfigError(f"unknown printer {printer!r}; known: {sorted(data['printers'])}")
    if material not in data["materials"]:
        raise ConfigError(f"unknown material {material!r}; known: {sorted(data['materials'])}")
    return ({"name": printer, **data["printers"][printer]}, {"name": material, **data["materials"][material]},
            data.get("slicer", {}))
