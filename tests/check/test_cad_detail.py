"""Fine detail, horizontal holes and flexing tabs (parts built from boxes: no triangulator needed)."""

from pathlib import Path

import numpy as np
import pytest
import trimesh

from proofloop.check.cad.checks import run_checks
from proofloop.check.cad.load import Model, load_profiles

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def profiles():
    return load_profiles(REPO / "src" / "proofloop" / "data", None, None)


def box(size, centre):
    b = trimesh.creation.box(size)
    b.apply_translation(centre)
    return b


def check(parts, profiles, use=None):
    mesh = trimesh.util.concatenate(parts) if isinstance(parts, list) else parts
    model = Model(path=Path("test.stl"), mesh=mesh, file_hash="t")
    printer, material, slicer = profiles
    return run_checks(model, printer, material, slicer, use or {})


def by(ctx, name):
    return [f for f in ctx.findings if f.check == name]


def test_raised_strokes_thinner_than_the_nozzle_are_found_but_wide_ones_are_not(profiles):
    parts = [box((40, 20, 3), (20, 10, 1.5))]
    parts += [box((0.25, 8, 0.6), (5 + 2 * i, 10, 3.3)) for i in range(3)]       # like thin text strokes
    parts.append(box((1.2, 8, 0.6), (30, 10, 3.3)))                               # printable ridge
    found = [f for f in by(check(parts, profiles), "fine_detail") if "Raised" in f.title]
    assert len(found) == 1 and found[0].data["width_mm"] < 0.4
    assert found[0].location["x"] < 12                                            # the strokes, not the ridge
    clean = by(check([parts[0], parts[-1]], profiles), "fine_detail")
    assert not clean


def test_engraved_groove_fills_in_but_a_gap_between_parts_is_a_clearance(profiles):
    base = box((30, 20, 2), (15, 10, 1))
    left, right = box((14.9, 20, 1), (7.45, 10, 2.5)), box((14.9, 20, 1), (22.55, 10, 2.5))   # 0.2 mm groove
    groove = [f for f in by(check([base, left, right], profiles), "fine_detail") if "Engraved" in f.title]
    assert groove and groove[0].data["width_mm"] < 0.4
    apart = check([box((14.9, 20, 3), (7.45, 10, 1.5)), box((14.9, 20, 3), (22.55, 10, 1.5))], profiles)
    assert not [f for f in by(apart, "fine_detail") if "Engraved" in f.title]


def test_steps_shallower_than_a_layer(profiles):
    parts = [box((30, 30, 3), (15, 15, 1.5)), box((8, 8, 0.1), (15, 15, 3.05))]
    step = [f for f in by(check(parts, profiles), "fine_detail") if "high" in f.title]
    assert step and step[0].data["step_mm"] == pytest.approx(0.1, abs=0.01)
    assert not [f for f in by(check([parts[0], box((8, 8, 0.6), (15, 15, 3.3))], profiles), "fine_detail")
                if "high" in f.title]


def _plate_with_side_holes(diameters, thickness=16.0, res=0.2):
    """A block with holes running along Y, built from thin boxes in X-Z rows."""
    width = 14.0 * len(diameters) + 6
    xs = np.arange(0, width, res) + res / 2
    centres = [(10 + 14 * i, thickness / 2, d) for i, d in enumerate(diameters)]
    boxes = []
    for z in np.arange(0, thickness, res) + res / 2:
        row = np.ones(len(xs), bool)
        for cx, cz, d in centres:
            row &= (xs - cx) ** 2 + (z - cz) ** 2 > (d / 2) ** 2
        edges = np.flatnonzero(np.diff(np.concatenate([[0], row.astype(int), [0]])))
        for a, b in zip(edges[::2], edges[1::2], strict=False):
            x0, x1 = xs[a] - res / 2, xs[b - 1] + res / 2
            boxes.append(box((x1 - x0, 20, res), ((x0 + x1) / 2, 10, z)))
    return boxes


def test_horizontal_holes_print_oval_or_sag(profiles):
    ctx = check(_plate_with_side_holes([5.0, 12.0]), profiles)
    holes = {round(f.data["diameter_mm"]): f for f in by(ctx, "horizontal_hole")}
    assert set(holes) == {5, 12}
    assert holes[5].severity == 1 and "oval" in holes[5].title
    assert holes[12].severity == 2 and "sags" in holes[12].title and holes[12].data["axis"] == "y"
    assert not by(check(box((40, 20, 16), (20, 10, 8)), profiles), "horizontal_hole")


def test_flexing_tab_gets_a_safe_deflection_and_upright_tabs_are_flagged(profiles):
    block = box((20, 20, 10), (10, 10, 5))
    side = check([block, box((20, 8, 1.6), (29.5, 10, 5))], profiles)                 # sticks out sideways
    tab = by(side, "cantilever")
    assert len(tab) == 1 and tab[0].severity == 1
    expected = 0.02 * tab[0].data["length_mm"] ** 2 / (1.5 * tab[0].data["thickness_mm"])
    assert tab[0].data["safe_deflection_mm"] == pytest.approx(expected, rel=0.01)
    assert 1.2 < tab[0].data["thickness_mm"] < 2.1
    upright = by(check([block, box((8, 1.6, 20), (10, 10, 19.5))], profiles), "cantilever")
    assert len(upright) == 1 and upright[0].severity == 3
    plate_on_block = by(check([block, box((20, 20, 1.6), (10, 10, 10.8))], profiles), "cantilever")
    assert not plate_on_block                                                      # held along a whole face
