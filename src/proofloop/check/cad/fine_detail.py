"""Detail finer than the nozzle or a layer: thin raised lines and text, narrow engravings, shallow steps."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import trimesh

from proofloop.check.cad.clearance import TOUCH_MM, shell_gap
from proofloop.check.cad.context import CadFinding, Context, _loc


def check_detail(ctx: Context) -> None:
    """Detail finer than the nozzle or a layer: thin raised lines and text, narrow engravings, shallow steps."""
    from proofloop.check.cad.detail import lost_detail, thin_steps

    printer = ctx.printer
    if printer.get("type", "fdm") != "fdm" or ctx.grid is None:
        return
    nozzle, layer = float(printer["nozzle"]), float(printer.get("layer_height", 0.2))
    mesh = ctx.mesh
    if "face_shell" not in ctx.cache:
        ctx.cache["face_shell"] = trimesh.graph.connected_component_labels(mesh.face_adjacency,
                                                                          node_count=len(mesh.faces))
    face_shell = ctx.cache["face_shell"]
    n_shells = int(face_shell.max()) + 1
    tri = mesh.triangles
    lo = np.full((n_shells, 3), np.inf)
    hi = np.full((n_shells, 3), -np.inf)
    np.minimum.at(lo, face_shell, tri.min(axis=1))
    np.maximum.at(hi, face_shell, tri.max(axis=1))
    order = np.argsort(face_shell, kind="stable")
    starts = np.searchsorted(face_shell[order], np.arange(n_shells + 1))
    touching: dict[tuple[int, int], bool] = {}

    def shell_mesh(k: int) -> trimesh.Trimesh:
        return trimesh.Trimesh(mesh.vertices, mesh.faces[order[starts[k]:starts[k + 1]]], process=False)

    def neighbours(k: int) -> list[int]:
        near = np.flatnonzero(np.all(lo <= hi[k] + TOUCH_MM, axis=1) & np.all(hi >= lo[k] - TOUCH_MM, axis=1))
        out = []
        for other in near:
            if other == k:
                continue
            key = (min(k, int(other)), max(k, int(other)))
            if key not in touching:
                touching[key] = shell_gap(shell_mesh(k), shell_mesh(int(other)), samples=4000)[0] < TOUCH_MM
            if touching[key]:
                out.append(int(other))
        return out

    def one_body(sets: list[set[int]]) -> bool:
        """Do the shells around each piece join up through shells that touch (overlap or share a face)?"""
        if n_shells == 1:
            return True
        start = next(iter(sets[0]))
        reached, frontier = {start}, [start]
        while frontier and len(reached) < 200:
            if all(s & reached for s in sets):
                return True
            for other in neighbours(frontier.pop()):
                if other not in reached:
                    reached.add(other)
                    frontier.append(other)
        return len(reached) >= 200 or all(s & reached for s in sets)   # very many shells: assume one body

    found = lost_detail(mesh, nozzle, layer, one_body, face_shell)
    if found["skipped"]:
        ctx.metrics["detail_note"] = found["skipped"][0]
    # thin walls are already reported; gaps between separate bodies were left out by one_body above
    near = [f.location for f in ctx.findings if f.location and f.check == "thin_wall"]

    def grouped(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for d in items:
            groups.setdefault((int(d["x"] // 4), int(d["y"] // 4)), []).append(d)
        out = []
        for g in groups.values():
            x, y = float(np.mean([d["x"] for d in g])), float(np.mean([d["y"] for d in g]))
            if any(math.hypot(x - loc["x"], y - loc["y"]) < 3 for loc in near):
                continue
            out.append({"x": x, "y": y, "z": float(np.median([d["z"] for d in g])), "n": len(g),
                        "width": float(min(d["width"] for d in g)), "area": float(sum(d["area"] for d in g))})
        return sorted(out, key=lambda g: -g["area"])

    thin, gaps = grouped(found["thin"]), grouped(found["gaps"])
    if thin:
        w = min(g["width"] for g in thin)
        top = thin[0]
        vanish = w < 0.6 * nozzle
        ctx.add(CadFinding("fine_detail", 2, "Raised detail thinner than the nozzle" + (" (text?)" if len(thin) >= 3 else ""),
                           f"{len(thin)} place(s) have raised lines or features about {w:.2f} mm wide; a {nozzle:g} mm "
                           "nozzle " + ("can't trace them, so the slicer drops them." if vanish else
                                        "traces them as a single thin line, so they come out blurry and fragile."),
                           f"Make raised lines and text strokes at least {1.5 * nozzle:.1f} mm wide (bold font, "
                           f"about 5 mm tall letters or more), or engrave them instead.",
                           _loc(np.array([top["x"], top["y"], top["z"]]), 4.0),
                           {"width_mm": round(w, 2), "places": len(thin)}))
    if gaps:
        w = min(g["width"] for g in gaps)
        top = gaps[0]
        ctx.add(CadFinding("fine_detail", 2, "Engraved detail narrower than the nozzle fills in",
                           f"{len(gaps)} place(s) have engraved lines or slots about {w:.2f} mm wide; the {nozzle:g} mm "
                           "nozzle's line is wider, so they close up or vanish.",
                           f"Make engraved lines at least {nozzle + 0.1:.1f} mm wide, ideally {2 * nozzle:.1f} mm.",
                           _loc(np.array([top["x"], top["y"], top["z"]]), 4.0),
                           {"width_mm": round(w, 2), "places": len(gaps)}))
    steps = thin_steps(ctx.mesh, layer)
    if steps:
        st = min(steps, key=lambda x: x["step"])
        ctx.add(CadFinding("fine_detail", 2, f"Detail only {st['step']:.2f} mm high: less than a layer",
                           f"{len(steps)} raised or sunk area(s) differ by only {st['step']:.2f} mm in height; at "
                           f"{layer:g} mm layers the slicer rounds that to zero or one layer, so it vanishes or "
                           "comes out a different depth.",
                           f"Make raised or engraved detail at least {2 * layer:.1f} mm ({2} layers) high or deep.",
                           _loc(st["centre"], 4.0), {"step_mm": round(st["step"], 3), "places": len(steps)}))
