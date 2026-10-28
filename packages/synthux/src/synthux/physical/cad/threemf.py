"""3MF reader (Fusion 360, Bambu Studio, PrusaSlicer, Windows 3D Builder) without extra packages.

Handles the core mesh spec, components (including Bambu/Prusa objects split into separate
model files with the production extension's p:path), build items with transforms, and units.
Only the parts marked printable on the build plate are loaded.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import trimesh

from synthux.core.config import ConfigError

CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
PROD_PATH = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
START_REL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
UNITS_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}


def _matrix(text: str | None) -> np.ndarray:
    m = np.eye(4)
    if text:
        v = [float(x) for x in text.split()]
        if len(v) == 12:  # 3MF stores the 4x3 matrix row by row, for row vectors: p' = [x y z 1] @ M
            m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
            m[:3, 3] = v[9:12]
    return m


class _Package:
    def __init__(self, path: Path):
        self.zip = zipfile.ZipFile(path)
        self.names = {n.lstrip("/").lower(): n for n in self.zip.namelist()}
        self.models: dict[str, tuple[ET.Element, float]] = {}

    def start_part(self) -> str:
        rels = self.names.get("_rels/.rels")
        if rels:
            root = ET.fromstring(self.zip.read(rels))
            for rel in root:
                if rel.get("Type") == START_REL:
                    return rel.get("Target", "").lstrip("/")
        for candidate in ("3d/3dmodel.model",):
            if candidate in self.names:
                return self.names[candidate]
        raise ConfigError("this 3MF has no 3D model in it (a sliced .gcode.3mf? use `synthux physical gcode`)")

    def model(self, part: str) -> tuple[ET.Element, float]:
        key = part.lstrip("/").lower()
        if key not in self.models:
            if key not in self.names:
                raise ConfigError(f"3MF refers to a missing part {part}")
            root = ET.fromstring(self.zip.read(self.names[key]))
            self.models[key] = (root, UNITS_MM.get(root.get("unit", "millimeter"), 1.0))
        return self.models[key]

    def objects(self, part: str) -> dict[str, ET.Element]:
        root, _ = self.model(part)
        res = root.find(f"{CORE}resources")
        return {o.get("id"): o for o in (res.findall(f"{CORE}object") if res is not None else [])}


def _collect(pkg: _Package, part: str, object_id: str, transform: np.ndarray,
             out: list[tuple[np.ndarray, np.ndarray]], depth: int = 0) -> None:
    if depth > 16:
        raise ConfigError("3MF components nest too deeply (a loop?)")
    obj = pkg.objects(part).get(object_id)
    if obj is None:
        raise ConfigError(f"3MF build refers to a missing object {object_id}")
    _, scale = pkg.model(part)
    mesh = obj.find(f"{CORE}mesh")
    if mesh is not None:
        verts = np.array([(float(v.get("x")), float(v.get("y")), float(v.get("z")))
                          for v in mesh.find(f"{CORE}vertices").iter(f"{CORE}vertex")], dtype=float) * scale
        faces = np.array([(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")))
                          for t in mesh.find(f"{CORE}triangles").iter(f"{CORE}triangle")], dtype=np.int64)
        if len(verts) and len(faces):
            homo = np.c_[verts, np.ones(len(verts))] @ transform.T
            out.append((homo[:, :3], faces))
    comps = obj.find(f"{CORE}components")
    for comp in (comps.findall(f"{CORE}component") if comps is not None else []):
        target = comp.get(PROD_PATH) or part
        _collect(pkg, target, comp.get("objectid"), transform @ _matrix(comp.get("transform")), out, depth + 1)


def read_3mf(path: Path | str) -> trimesh.Trimesh:
    path = Path(path)
    try:
        pkg = _Package(path)
    except zipfile.BadZipFile as exc:
        raise ConfigError(f"{path.name} is not a valid 3MF (zip) file") from exc
    part = pkg.start_part()
    root, _ = pkg.model(part)
    build = root.find(f"{CORE}build")
    items = build.findall(f"{CORE}item") if build is not None else []
    if not items:
        raise ConfigError(f"{path.name}: the 3MF build plate is empty")
    pieces: list[tuple[np.ndarray, np.ndarray]] = []
    for item in items:
        if item.get("printable", "1") in ("0", "false"):
            continue
        _collect(pkg, item.get(PROD_PATH) or part, item.get("objectid"), _matrix(item.get("transform")), pieces)
    if not pieces:
        raise ConfigError(f"{path.name} contains no triangles")
    offset, verts, faces = 0, [], []
    for v, f in pieces:
        verts.append(v)
        faces.append(f + offset)
        offset += len(v)
    return trimesh.Trimesh(np.vstack(verts), np.vstack(faces), process=True)
