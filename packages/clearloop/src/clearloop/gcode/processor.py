"""Turn a sliced plate into a loop-ready file."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .. import __version__, manifest, protocol
from ..profiles import MaterialProfile, PrinterProfile, combine_materials
from ..threemf import SlicedFile
from .heightmap import HeightMap, ObjectBox, build_heightmap
from .routines import (
    LoopSettings,
    block,
    loop_block,
    loop_end_block,
    natural_cooldown_minutes,
    override_bed_temp,
    start_block,
    strip_blocks,
)
from .sweep import SweepPlan, plan_sweep


class UnsupportedMaterial(ValueError):
    pass


@dataclass
class ProcessOptions:
    printer: PrinterProfile
    loop: LoopSettings = field(default_factory=LoopSettings)
    copies: int = 1
    bed_temp: float | None = None
    secret: str | None = None
    allow_unsupported: bool = False
    heightmap_cell: float = 2.0


@dataclass
class ProcessResult:
    data: bytes
    gcode: str
    manifest: dict
    objects: list[ObjectBox]
    plan: SweepPlan
    material: MaterialProfile
    heightmap: HeightMap
    warnings: list[str]
    cycle_seconds: float


def _normalize_model(s: str) -> str:
    return s.lower().replace("bambu lab", "").replace(" ", "").replace("-", "")


def analyze(
    sliced: SlicedFile, opts: ProcessOptions
) -> tuple[str, HeightMap, list[ObjectBox], SweepPlan, MaterialProfile, list[str]]:
    original = strip_blocks(sliced.gcode)
    original = "".join(
        line for line in original.splitlines(keepends=True) if not line.startswith(manifest.LINE_PREFIX)
    )
    warnings: list[str] = []
    printer = opts.printer

    material = combine_materials(sliced.info.filaments)
    if not material.supported:
        msg = f"{material.name} is not supported for automatic ejection: {material.reason}"
        if not opts.allow_unsupported:
            raise UnsupportedMaterial(msg)
        warnings.append(msg)
    if not sliced.info.filaments:
        warnings.append("no filament type found in the file; using PLA cooldown defaults")

    if sliced.info.printer_model and _normalize_model(sliced.info.printer_model) != printer.model:
        warnings.append(
            f"file was sliced for {sliced.info.printer_model!r} but is being processed for {printer.model}"
        )

    hmap = build_heightmap(original, printer.bed_x, printer.bed_y, opts.heightmap_cell)
    objects = hmap.objects()
    if not objects:
        warnings.append("no printed geometry found; the sweep plan is empty")
    if hmap.max_height > printer.max_z - 10:
        warnings.append("parts are close to the maximum build height; check sweep clearance")

    plan = plan_sweep(objects, printer, opts.loop.sweep)
    warnings += plan.warnings
    if opts.loop.eject_mode == "gcode":
        for i, o in enumerate(objects):
            if o.footprint_mm2 > 10_000:
                warnings.append(
                    f"object {i} has a {o.footprint_mm2 / 100:.0f} cm2 footprint; large flat parts often "
                    "need a bender to release"
                )
    return original, hmap, objects, plan, material, warnings


def process(sliced: SlicedFile, opts: ProcessOptions) -> ProcessResult:
    if opts.copies < 1:
        raise ValueError("copies must be at least 1")
    original, hmap, objects, plan, material, warnings = analyze(sliced, opts)
    s = opts.loop
    printer = opts.printer

    body = original
    if opts.bed_temp is not None:
        body = override_bed_temp(body, opts.bed_temp)

    one = start_block(s) + body + loop_block(plan, printer, material, s, sliced.info.chamber_temp)
    gcode = one
    for _ in range(opts.copies - 1):
        gcode += block("COPY", loop_end_block(s) + one)

    natural = natural_cooldown_minutes(s, material, printer, sliced.info.chamber_temp)
    per_cycle = (
        sliced.info.print_seconds
        + natural * 60
        + s.min_cooldown_s
        + (s.cooldown_time_s if s.cooldown_mode == "time" else 240)
        + (s.bend_window_s if s.eject_mode == "board" else 0)
        + plan.est_seconds
    )

    data = {
        "tool": "clearloop",
        "tool_version": __version__,
        "protocol": protocol.PROTOCOL_VERSION,
        "marker_base": s.marker_base,
        "created": int(time.time()),
        "printer": printer.model,
        "plate": sliced.plate,
        "eject_mode": s.eject_mode,
        "copies": opts.copies,
        "material": material.name,
        "release_temp": s.release_temp if s.release_temp is not None else material.release_temp,
        "natural_cooldown_min": natural,
        "objects": [o.as_dict() for o in objects],
        "sweep_passes": len(plan.passes),
        "cycle_seconds_est": round(per_cycle),
        "warnings": warnings,
    }
    data["gcode_sha256"] = manifest.gcode_digest(gcode)
    signed = manifest.sign(data, opts.secret)
    gcode = manifest.embed(gcode, signed)
    out = sliced.to_bytes(gcode, extra={manifest.ENTRY_NAME: json.dumps(signed, indent=2).encode()})
    return ProcessResult(out, gcode, signed, objects, plan, material, hmap, warnings, per_cycle)
