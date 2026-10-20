"""Geometry-aware sweep planning.

A single push height (e.g. "tallest part minus 10 mm") sails straight over short
parts on a mixed plate. Instead every object gets its own lanes and push height,
objects nearest the exit edge are cleared first, and travel always clears the
tallest part still standing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..profiles import PrinterProfile
from .heightmap import ObjectBox


@dataclass
class SweepSettings:
    lane_spacing: float = 25.0  # max distance between push lanes across one part
    clearance: float = 6.0  # how far behind a part's back edge the nozzle drops
    push_ratio: float = 0.3  # push at this fraction of part height...
    max_push_z: float = 12.0  # ...but never higher than this
    top_margin: float = 1.0  # stay at least this far below a part's top
    travel_margin: float = 5.0  # travel this far above the tallest standing part
    final_wipe: bool = False  # extra low passes across the whole plate (tipped parts)
    wipe_spacing: float = 30.0


@dataclass
class SweepPass:
    x: float
    y_start: float
    y_end: float
    push_z: float
    travel_z: float
    target: int | None  # index into the object list, None for a wipe pass

    def as_dict(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class SweepPlan:
    passes: list[SweepPass] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    est_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "passes": [p.as_dict() for p in self.passes],
            "warnings": self.warnings,
            "est_seconds": round(self.est_seconds, 1),
        }


def push_height(height: float, profile: PrinterProfile, s: SweepSettings) -> tuple[float, str | None]:
    if height < profile.min_sweep_z + 0.3:
        return profile.min_sweep_z, (
            f"part is only {height:.1f} mm tall, at or below the {profile.min_sweep_z} mm sweep floor; "
            "it may be left behind (bed verification will catch it)"
        )
    z = min(height * s.push_ratio, s.max_push_z, height - s.top_margin)
    return max(profile.min_sweep_z, z), None


def plan_sweep(
    objects: list[ObjectBox], profile: PrinterProfile, s: SweepSettings | None = None
) -> SweepPlan:
    s = s or SweepSettings()
    plan = SweepPlan()
    # Front-most first, so a rear part is never pushed into one still standing.
    order = sorted(range(len(objects)), key=lambda i: (objects[i].ymin, objects[i].xmin))
    standing = set(order)
    for idx in order:
        obj = objects[idx]
        z, warn = push_height(obj.height, profile, s)
        if warn:
            plan.warnings.append(f"object {idx} at x={obj.xmin:.0f}-{obj.xmax:.0f}: {warn}")
        travel = max(objects[i].height for i in standing) + s.travel_margin
        n = max(1, math.ceil(obj.width / s.lane_spacing))
        for lane in range(n):
            x = obj.xmin + obj.width * (lane + 0.5) / n
            plan.passes.append(
                SweepPass(
                    x=_clamp(x, 0, profile.bed_x),
                    y_start=min(obj.ymax + s.clearance, profile.bed_y),
                    y_end=profile.eject_y,
                    push_z=z,
                    travel_z=min(travel, profile.max_z),
                    target=idx,
                )
            )
        standing.discard(idx)
    if s.final_wipe:
        n = max(1, math.ceil(profile.bed_x / s.wipe_spacing))
        for lane in range(n):
            plan.passes.append(
                SweepPass(
                    x=profile.bed_x * (lane + 0.5) / n,
                    y_start=profile.bed_y,
                    y_end=profile.eject_y,
                    push_z=profile.min_sweep_z,
                    travel_z=profile.min_sweep_z + s.travel_margin,
                    target=None,
                )
            )
    plan.est_seconds = estimate_seconds(plan, profile)
    return plan


def estimate_seconds(plan: SweepPlan, profile: PrinterProfile) -> float:
    total = 0.0
    x, y = profile.bed_x / 2, profile.bed_y
    for p in plan.passes:
        total += abs(p.travel_z - p.push_z) * 2 / (profile.z_feed / 60)
        total += math.hypot(p.x - x, p.y_start - y) / (profile.travel_feed / 60)
        total += abs(p.y_start - p.y_end) / (profile.push_feed / 60)
        x, y = p.x, p.y_end
    return total


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
