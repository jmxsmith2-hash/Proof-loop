"""Sweep planning on top of the FarmLoop hardware's own sweep path.

The lanes, rear/front positions, feeds and park moves come from the hardware
profile, i.e. from FarmLoop's own output for that kit. What ProofLoop changes is
the push height. The FarmLoop app uses one height for the whole plate: "the
maximum part height minus 10 mm" (its Detachment Tuning text). With a 170 mm and
a 5 mm part on one plate that is Z160, which never touches the 5 mm part. That's
the documented "short parts left behind" complaint.

ProofLoop applies the same rule per part. Each lane is pushed once for every
distinct part height in it, tallest first, at that part's height minus the
offset, but never below your calibrated sweep floor. Lanes with no parts keep
the vendor behaviour (one pass at the plate's tallest push height), so the
result is never less thorough than FarmLoop's.

Safety margins used here (travel clearance, rear clearance, tier merging) are
ProofLoop design choices, not hardware facts, and are exposed as settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from proofloop.loopfiles.gcode.objects import Part
from proofloop.loopfiles.profiles import SweepGeometry


@dataclass
class SweepSettings:
    push_offset: float = 10.0  # FarmLoop: push height = part height minus 10 mm
    floor_z: float = 2.0  # lowest Z the pusher may sweep at; calibrate it (see the Calibrate tab)
    travel_margin: float = 5.0  # travel this far above the tallest part
    rear_clearance: float = 10.0  # warn when a part reaches within this distance of the sweep's start line
    merge_mm: float = 1.0  # tiers closer than this are swept once, at the lower height
    # Your own push height for individual parts (label -> Z), set with the sliders in the app.
    # Never applied below floor_z: the floor protects the plate.
    part_push_z: dict[str, float] = field(default_factory=dict)


@dataclass
class LanePlan:
    x: float
    tiers: list[float]  # push heights, highest first
    parts: list[str]  # labels of parts in this lane


@dataclass
class PassPlan:
    feed: int
    lanes: list[LanePlan]


@dataclass
class SweepPlan:
    passes: list[PassPlan]
    travel_z: float
    coverage: tuple[float, float]
    part_push: dict[str, float] = field(default_factory=dict)  # label -> lowest push height reaching it
    part_target: dict[str, float] = field(default_factory=dict)  # label -> the push height planned for it
    part_custom: set[str] = field(default_factory=set)  # labels whose height you set yourself
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # blocking: the file is not safe to produce

    def as_dict(self) -> dict:
        return {
            "travel_z": round(self.travel_z, 2),
            "coverage": [round(c, 1) for c in self.coverage],
            "passes": [
                {
                    "feed": p.feed,
                    "lanes": [
                        {"x": ln.x, "tiers": [round(t, 2) for t in ln.tiers], "parts": ln.parts}
                        for ln in p.lanes
                    ],
                }
                for p in self.passes
            ],
            "part_push": {k: round(v, 2) for k, v in self.part_push.items()},
            "part_target": {k: round(v, 2) for k, v in self.part_target.items()},
            "part_custom": sorted(self.part_custom),
            "warnings": self.warnings,
            "problems": self.problems,
        }


def target_z(p: Part, s: SweepSettings) -> float:
    """The push height for one part: yours if you set one, else FarmLoop's rule (height minus offset)."""
    z = s.part_push_z.get(p.label)
    if z is None:
        z = p.height - s.push_offset
    return max(s.floor_z, z)


def _tiers(zs_in: list[float], s: SweepSettings) -> list[float]:
    zs = sorted(set(zs_in), reverse=True)
    out: list[float] = []
    for z in zs:
        if out and out[-1] - z < s.merge_mm:
            out[-1] = z  # merge into one pass at the lower height
        else:
            out.append(z)
    return out


def plan_sweep(
    parts: list[Part], geo: SweepGeometry, printable_height: float, s: SweepSettings | None = None
) -> SweepPlan:
    s = s or SweepSettings()
    half = geo.lane_pitch / 2
    lanes = geo.lanes
    cov = (min(lanes) - half, max(lanes) + half)
    tallest = max((p.height for p in parts), default=0.0)
    if printable_height <= 0:
        raise ValueError("the slice does not state the printer's printable height")
    travel = min(printable_height, tallest + s.travel_margin)
    plan = SweepPlan(passes=[], travel_z=travel, coverage=cov)

    if tallest + s.travel_margin > printable_height:
        plan.warnings.append(
            f"the tallest part ({tallest:.1f} mm) leaves less than {s.travel_margin:g} mm of travel clearance "
            f"under the printer's {printable_height:g} mm height; travel moves use Z{printable_height:g}"
        )
    targets = {p.label: target_z(p, s) for p in parts}
    plan.part_target = dict(targets)
    plate_top_tier = max(targets.values()) if parts else s.floor_z

    for p in parts:
        custom = s.part_push_z.get(p.label)
        if custom is not None:
            plan.part_custom.add(p.label)
            if custom < s.floor_z:
                plan.warnings.append(
                    f"{p.name}: you set Z{custom:g}, below your sweep floor; it is pushed at the floor (Z{s.floor_z:g}) instead"
                )
            if targets[p.label] >= p.height:
                # Leaving a part behind means the next loop prints onto it: block, don't just warn.
                plan.problems.append(
                    f"{p.name}: the push height you set (Z{targets[p.label]:g}) is not below the part's top "
                    f"({p.height:.1f} mm), so the pusher would pass over it. Lower it or reset it to automatic."
                )
        if p.height < s.floor_z:
            plan.warnings.append(
                f"{p.name} is {p.height:.1f} mm tall, lower than your sweep floor (Z{s.floor_z:g}); the pusher "
                "passes over it. Lower the floor after calibrating, or remove this part by hand."
            )
        if p.xmin < cov[0] or p.xmax > cov[1]:
            plan.warnings.append(
                f"{p.name} spans X{p.xmin:.0f}-{p.xmax:.0f}, outside the sweep lanes (X{cov[0]:.0f}-{cov[1]:.0f} "
                "with the kit's lane spacing); part of it may not be pushed. Move it toward the middle."
            )
        if p.ymax > geo.y_rear - s.rear_clearance:
            plan.problems.append(
                f"{p.name} reaches Y{p.ymax:.0f}; the sweep drops down at Y{geo.y_rear:g} behind the parts and "
                f"needs {s.rear_clearance:g} mm of clearance. Move the part forward on the plate."
            )

    for vp in geo.passes:
        pass_plan = PassPlan(vp.feed, [])
        for x in vp.lanes:
            inside = [p for p in parts if p.xmax >= x - half and p.xmin <= x + half]
            tiers = _tiers([targets[p.label] for p in inside], s) if inside else [plate_top_tier]
            pass_plan.lanes.append(LanePlan(x, tiers, [p.label for p in inside]))
            for p in inside:
                lowest = min(tiers)
                plan.part_push[p.label] = min(plan.part_push.get(p.label, lowest), lowest)
        plan.passes.append(pass_plan)
    return plan


def sweep_gcode(plan: SweepPlan, geo: SweepGeometry) -> str:
    """Sideways moves only happen at the rear line, behind every part; each lane is pushed rear to front."""
    out = [
        f"G1 Z{plan.travel_z:.2f} F{geo.z_feed} ; travel height clears the tallest part",
        f"G1 Y{geo.y_rear:g} F{geo.passes[0].feed} ; go behind the parts",
    ]
    for i, p in enumerate(plan.passes, 1):
        out.append(f"; -- sweep pass {i}/{len(plan.passes)} at F{p.feed} --")
        for ln in p.lanes:
            out.append(f"G1 X{ln.x:g} F{p.feed}")
            for z in ln.tiers:
                out.append(f"G1 Z{z:.2f} F{geo.z_feed}")
                out.append(f"G1 Y{geo.y_front:g} F{p.feed}")
                out.append(f"G1 Y{geo.y_rear:g} F{p.feed}")
    out.append(f"G1 Z{plan.travel_z:.2f} F{geo.z_feed} ; lift clear before parking")
    out += geo.park
    return "\n".join(out) + "\n"


def sweep_seconds(plan: SweepPlan, geo: SweepGeometry, max_xy: float, max_z: float) -> float:
    """Motion time of the sweep at the commanded feeds, capped by the printer's own speed limits (mm/s)."""

    def t(dist: float, feed: float, cap: float) -> float:
        v = min(feed / 60.0, cap) if cap > 0 else feed / 60.0
        return dist / v if v > 0 else 0.0

    total = 0.0
    depth = abs(geo.y_rear - geo.y_front)
    z = plan.travel_z
    x = None
    for p in plan.passes:
        for ln in p.lanes:
            if x is not None:
                total += t(abs(ln.x - x), p.feed, max_xy)
            x = ln.x
            for tz in ln.tiers:
                total += t(abs(z - tz), geo.z_feed, max_z)
                z = tz
                total += 2 * t(depth, p.feed, max_xy)
    return total


def sweep_path(plan: SweepPlan, geo: SweepGeometry) -> list[dict]:
    """The sweep as a list of toolhead positions, read back from the exact G-code ProofLoop writes.

    The toolhead's XY position before the sweep depends on the printer's own end G-code, so the
    path starts behind the plate at the first lane; every move after that is the file's own.
    """
    import re

    word = re.compile(r"([XYZF])\s*([-+]?\d+\.?\d*)")
    first_x = plan.passes[0].lanes[0].x if plan.passes and plan.passes[0].lanes else 0.0
    pos = {"X": first_x, "Y": geo.y_rear, "Z": plan.travel_z, "F": float(geo.z_feed)}
    path = []
    for line in sweep_gcode(plan, geo).splitlines():
        code = line.split(";", 1)[0].strip()
        if not code.startswith(("G1 ", "G0 ")):
            continue
        w = {k: float(v) for k, v in word.findall(code)}
        pos = {**pos, **w}
        push = "Y" in w and w["Y"] == geo.y_front
        path.append({"x": pos["X"], "y": pos["Y"], "z": round(pos["Z"], 2), "f": pos["F"], "push": push})
    return path
