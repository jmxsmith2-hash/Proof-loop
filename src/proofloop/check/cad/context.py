"""What the CAD checks share: the findings, the context they run on, and small geometry helpers.

Each finding has a location in millimetres (model coordinates), a severity, and a suggested fix.
Severity: 1 note, 2 minor, 3 major, 4 will fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from proofloop.check.cad.load import Model
from proofloop.check.cad.voxel import VoxelGrid

SEVERITY = {1: "note", 2: "minor", 3: "major", 4: "will fail"}


@dataclass
class CadFinding:
    check: str
    severity: int
    title: str
    detail: str
    fix: str = ""
    location: dict[str, float] | None = None      # {"x","y","z"} centre in mm, plus optional "r" radius
    data: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""

    @property
    def severity_name(self) -> str:
        return SEVERITY[self.severity]


@dataclass
class Context:
    model: Model
    printer: dict[str, Any]
    material: dict[str, Any]
    slicer: dict[str, Any]
    use: dict[str, Any] = field(default_factory=dict)  # handheld, wearable, load, ...
    grid: VoxelGrid | None = None
    findings: list[CadFinding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    exterior: np.ndarray | None = None              # faces on the real outer surface (not buried in another body)
    cache: dict[str, Any] = field(default_factory=dict, repr=False)   # shared intermediate results (distance maps)

    @property
    def mesh(self) -> trimesh.Trimesh:
        return self.model.mesh

    @property
    def voxels(self) -> VoxelGrid:
        """The voxel grid, for checks that cannot run without it."""
        if self.grid is None:
            raise RuntimeError("the voxel grid has not been built")
        return self.grid

    def add(self, finding: CadFinding) -> None:
        if not finding.fingerprint:
            finding.fingerprint = fingerprint(finding, self.mesh.bounds)
        for existing in self.findings:  # one finding per problem and place
            if existing.fingerprint == finding.fingerprint and finding.location:
                if finding.severity > existing.severity:
                    self.findings[self.findings.index(existing)] = finding
                return
        self.findings.append(finding)


def fingerprint(f: CadFinding, bounds: np.ndarray) -> str:
    """Stable id for matching the same problem across design versions (location bucketed to a 4x4x4 grid)."""
    if not f.location:
        return f.check
    lo, hi = bounds
    rel = (np.array([f.location["x"], f.location["y"], f.location["z"]]) - lo) / np.maximum(hi - lo, 1e-6)
    bucket = np.clip((rel * 4).astype(int), 0, 3)
    return f"{f.check}@{bucket[0]}{bucket[1]}{bucket[2]}"


def _loc(point: np.ndarray, radius: float | None = None) -> dict[str, float]:
    out = {"x": round(float(point[0]), 1), "y": round(float(point[1]), 1), "z": round(float(point[2]), 1)}
    if radius is not None:
        out["r"] = round(float(radius), 1)
    return out


def _face_components(mesh: trimesh.Trimesh, mask: np.ndarray) -> list[np.ndarray]:
    """Connected groups of the faces selected by mask (sharing an edge)."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    pairs = mesh.face_adjacency
    keep = mask[pairs[:, 0]] & mask[pairs[:, 1]]
    p = pairs[keep]
    remap = -np.ones(len(mesh.faces), dtype=int)
    remap[idx] = np.arange(idx.size)
    graph = coo_matrix((np.ones(len(p)), (remap[p[:, 0]], remap[p[:, 1]])), shape=(idx.size, idx.size))
    n, labels = connected_components(graph, directed=False)
    return [idx[labels == k] for k in range(n)]


def exterior_faces(mesh: trimesh.Trimesh, grid: VoxelGrid) -> np.ndarray:
    """Faces whose outside is air. Faces buried inside an overlapping body, or pressed against another
    body's surface, are not part of what gets printed and must not be judged."""
    probe = mesh.triangles_center + mesh.face_normals * grid.pitch * 1.2
    return grid.is_outside(probe)
