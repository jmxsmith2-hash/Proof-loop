"""Generate the example CAD files (run: python examples/cad/make_examples.py).

phone_stand_v1.stl  first design, with deliberate problems (ground truth: planted_flaws.yaml)
phone_stand_v2.stl  the refined design with those problems fixed
block_control.stl   a plain sturdy block that should pass cleanly
tool_handle.stl     a handheld tool grip, for the hand-size personas (see tool_handle.yaml)
"""

from pathlib import Path

import numpy as np
import trimesh

HERE = Path(__file__).parent


def box(size, centre):
    b = trimesh.creation.box(size)
    b.apply_translation(centre)
    return b


def hull(points):
    return trimesh.Trimesh(vertices=np.array(points, dtype=float)).convex_hull


def back_plate(thickness: float, lean_deg: float = 15.0):
    plate = box((60, thickness, 90), (0, 0, 45))
    plate.apply_transform(trimesh.transformations.rotation_matrix(np.radians(-lean_deg), [1, 0, 0]))
    plate.apply_translation((0, 6, 2))
    return plate


def v1():
    parts = [
        box((60, 25, 4), (0, 0, 2)),                       # base: too shallow for a leaning phone
        back_plate(0.5),                                   # PLANTED thin wall (0.5 mm)
        box((60, 6, 6), (0, -9, 7)),                       # front lip that holds the phone
        hull([[-15, 6, 78], [15, 6, 78], [-15, 6, 84], [15, 6, 84],     # PLANTED hood with a 65° underside
              [-15, -12, 86], [15, -12, 86], [-15, -12, 84.5], [15, -12, 84.5]]),
        hull([[22, -8, 0], [28, -8, 0], [25, -20, 0],                     # PLANTED knife-edge cable guide
              [22, -8, 8], [28, -8, 8], [25, -20, 8]]),
        trimesh.creation.cylinder(radius=1.5, height=22, sections=24),   # PLANTED slender upright pin
        box((10, 6, 4), (-8, -15.6, 2)),                   # PLANTED separate clip 0.1 mm from the base
    ]
    parts[5].apply_translation((-24, -6, 14.5))           # sunk 0.5 mm into the base
    return trimesh.util.concatenate(parts)


def v2():
    parts = [
        box((70, 70, 5), (0, 15, 2.5)),                    # deep base reaching under the phone's weight
        back_plate(3.0),                                   # 3 mm wall
        box((60, 6, 6), (0, -9, 7.5)),
        trimesh.creation.cylinder(radius=4, height=22, sections=32),    # thick pin
        box((10, 6, 4), (-8, -23.5, 2)),                   # clip moved 0.5 mm from the base
    ]
    parts[3].apply_translation((-24, -6, 14.5))
    return trimesh.util.concatenate(parts)


def tool_handle():
    """A handle for a hand tool with a battery inside: 52 mm across (too thick for small hands)."""
    grip = trimesh.creation.cylinder(radius=26, height=130, sections=64)
    grip.apply_translation((0, 0, 65))
    cap = trimesh.creation.cylinder(radius=30, height=8, sections=64)
    cap.apply_translation((0, 0, 4))
    return trimesh.util.concatenate([cap, grip])


def control():
    return box((50, 30, 20), (0, 0, 10))


if __name__ == "__main__":
    v1().export(HERE / "phone_stand_v1.stl")
    v2().export(HERE / "phone_stand_v2.stl")
    control().export(HERE / "block_control.stl")
    tool_handle().export(HERE / "tool_handle.stl")
    print("written to", HERE)
