from pathlib import Path

import numpy as np
import pytest
import trimesh

from synthux.core.config import ConfigError
from synthux.personas.models import Persona, Traits
from synthux.physical.cad.checks import point_triangle_distance, run_checks
from synthux.physical.cad.ergonomics import grip_facts, section_profile
from synthux.physical.cad.iterate import analyse_print, apply_learned, compare_versions
from synthux.physical.cad.load import guess_units, load_model, load_profiles
from synthux.physical.cad.personas import product_types, react
from synthux.physical.cad.voxel import voxelize

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "config"


@pytest.fixture(scope="module")
def profiles():
    return load_profiles(CFG, None, None)


def test_voxelizer_is_exact_for_boxes_overlaps_and_hollows():
    box = trimesh.creation.box((40, 20, 10))
    assert voxelize(box, 0.5).volume_mm3 == pytest.approx(8000)
    overlap = trimesh.util.concatenate([box, trimesh.creation.box((10, 10, 30))])
    assert voxelize(overlap, 0.5).volume_mm3 == pytest.approx(10000)   # overlapping shells = one solid
    inner = trimesh.creation.box((16, 16, 16))
    inner.invert()
    hollow = trimesh.util.concatenate([trimesh.creation.box((20, 20, 20)), inner])
    assert voxelize(hollow, 0.25).volume_mm3 == pytest.approx(20 ** 3 - 16 ** 3)


def test_point_triangle_distance_matches_brute_force():
    rng = np.random.default_rng(1)
    tris = rng.uniform(-5, 5, (50, 3, 3))
    p = np.array([1.0, 2.0, 0.5])
    exact = point_triangle_distance(p, tris)
    for t, d in zip(tris, exact):
        u, v = np.meshgrid(np.linspace(0, 1, 120), np.linspace(0, 1, 120))
        keep = u + v <= 1
        pts = t[0] + np.outer(u[keep], t[1] - t[0]) + np.outer(v[keep], t[2] - t[0])
        assert d <= np.linalg.norm(pts - p, axis=1).min() + 1e-9
        assert d == pytest.approx(np.linalg.norm(pts - p, axis=1).min(), abs=0.08)


def test_units_guess_and_override(tmp_path):
    assert guess_units(np.array([0.05, 0.02, 0.01]))[0] == 1000.0       # metres
    assert "inches" in guess_units(np.array([4.0, 2.0, 1.0]))[1]
    trimesh.creation.box((0.05, 0.02, 0.01)).export(tmp_path / "m.stl")
    assert load_model(tmp_path / "m.stl").size[0] == pytest.approx(50)
    trimesh.creation.box((2, 1, 1)).export(tmp_path / "i.stl")
    assert load_model(tmp_path / "i.stl", units="in").size[0] == pytest.approx(50.8)
    with pytest.raises(ConfigError):
        load_model(tmp_path / "missing.stl")


def test_too_big_for_printer(tmp_path, profiles):
    printer, material, slicer = profiles
    trimesh.creation.box((300, 30, 30)).export(tmp_path / "long.stl")
    ctx = run_checks(load_model(tmp_path / "long.stl"), printer, material, slicer)
    assert any(f.check == "build_volume" and f.severity == 4 for f in ctx.findings)
    trimesh.creation.box((100, 100, 240)).export(tmp_path / "tall.stl")
    ctx = run_checks(load_model(tmp_path / "tall.stl"), {**printer, "build_volume": [250, 250, 200]}, material, slicer)
    assert any(f.check == "build_volume" and f.severity == 2 for f in ctx.findings)   # fits on its side


def test_stability_with_and_without_load(tmp_path, profiles):
    printer, material, slicer = profiles
    trimesh.creation.box((40, 40, 20)).export(tmp_path / "b.stl")
    model = load_model(tmp_path / "b.stl")
    assert not any(f.check == "stability" for f in run_checks(model, printer, material, slicer).findings)
    heavy_edge = {"load": {"mass_g": 500, "at": [30, 0, 60]}}
    ctx = run_checks(model, printer, material, slicer, heavy_edge)
    assert any(f.check == "stability" and f.severity == 4 for f in ctx.findings)


def test_sections_and_grip():
    cyl = trimesh.creation.cylinder(radius=18, height=120, sections=64)
    prof = section_profile(cyl, 2, 10.0)
    assert prof["perimeter_mm"] == pytest.approx(2 * np.pi * 18, rel=0.01)
    assert grip_facts(cyl)["grip_max_mm"] == pytest.approx(36, abs=0.5)


def test_print_analysis_and_learning():
    rows = [{"feature": "w", "designed_mm": 50, "measured_mm": 49.75, "kind": "outer", "axis": "x"},
            {"feature": "hole", "designed_mm": 5, "measured_mm": 4.7, "kind": "hole", "axis": ""},
            {"feature": "gap", "designed_mm": 0.3, "measured_mm": 0.15, "kind": "gap", "axis": ""}]
    a = analyse_print(rows, ["poor_overhang", "too_tight"])
    assert a["scale"]["x"] == pytest.approx(0.995) and a["hole_offset_mm"] == pytest.approx(-0.3)
    assert a["adjust"] == {"clearance_mm": 0.15, "max_overhang_deg": -5}    # too_tight not double counted
    tuned = apply_learned({"max_overhang_deg": 45, "clearance_mm": 0.3, "min_wall_mm": 0.8, "max_bridge_mm": 25},
                          {"prints": 1, "adjust": a["adjust"]})
    assert tuned["max_overhang_deg"] == 40 and tuned["clearance_mm"] == pytest.approx(0.45)


def test_version_comparison_matches_by_check_and_place():
    prev = [{"check": "overhang", "location": {"x": 0, "y": 0, "z": 80}, "severity": 3, "title": "a"},
            {"check": "thin_wall", "location": {"x": 10, "y": 0, "z": 40}, "severity": 3, "title": "b"}]
    new = [{"check": "overhang", "location": {"x": 2, "y": 1, "z": 78}, "severity": 2, "title": "a"},
           {"check": "sharp_edge", "location": {"x": 0, "y": 0, "z": 0}, "severity": 2, "title": "c"}]
    c = compare_versions(prev, new, 90)
    assert [f["title"] for f in c["fixed"]] == ["b"]
    assert [f["title"] for f in c["persisting"]] == ["a"] and c["persisting"][0]["was_severity"] == 3
    assert [f["title"] for f in c["new"]] == ["c"]


def test_reactions_need_a_measured_fact():
    p = Persona(id="p", label="", narrative="", traits=Traits(hand_size="small"))
    facts = {"mass_g": 100, "handheld": True, "wearable": None, "findings": {},
             "grip": {"grip_min_mm": 30, "grip_max_mm": 34, "axis": "z", "length_mm": 100, "max_width_mm": 34}}
    assert react(p, facts, product_types(["everyday"], {"handheld": True})) == []    # nothing wrong, nothing said
    facts["grip"]["grip_max_mm"] = 50
    out = react(p, facts, product_types(["everyday"], {"handheld": True}))
    assert out[0].pattern == "grip_too_big" and out[0].text.startswith("Prediction, not a test")
    assert "38 mm" in out[0].evidence[0] or "38" in out[0].evidence[0]


def test_wearable_fit():
    big = Persona(id="b", label="", narrative="", traits=Traits(hand_size="large"))
    small = Persona(id="s", label="", narrative="", traits=Traits(hand_size="small"))
    facts = {"mass_g": 20, "handheld": False, "wearable": "wrist", "findings": {},
             "fit": {"body": "wrist", "inner_mm": 160, "adjustable_mm": 0, "fits_sizes": ["small"],
                     "population": {"small": 140, "medium": 165, "large": 190}}}
    types = product_types(["wearable"], {"wearable": "wrist"})
    assert react(big, facts, types)[0].pattern == "doesnt_fit"
    assert not any(r.pattern == "doesnt_fit" for r in react(small, facts, types))


def test_support_is_measured_down_to_the_bed_or_the_part_below(tmp_path, profiles):
    printer, material, slicer = profiles
    post = trimesh.creation.box((10, 10, 40))
    post.apply_translation((0, 0, 20))
    high = trimesh.creation.box((30, 10, 4))          # a 30 x 10 mm ledge beside the post, 36 mm above the bed
    high.apply_translation((20, 0, 38))
    trimesh.util.concatenate([post, high]).export(tmp_path / "ledge.stl")
    ctx = run_checks(load_model(tmp_path / "ledge.stl"), printer, material, slicer)
    column = ctx.metrics["support_column_mm3"]
    assert 0.9 * (30 * 10 * 36) <= column <= 1.1 * (30 * 10 * 36)
    est = ctx.metrics["estimates"]
    assert est["support_grams"] > 0 and est["filament_grams"] > est["printed_grams"]   # support costs, not weighs
    low = high.copy()
    low.apply_translation((0, 0, -30))                 # the same ledge only 6 mm up needs a much shorter column
    trimesh.util.concatenate([post, low]).export(tmp_path / "low.stl")
    assert run_checks(load_model(tmp_path / "low.stl"), printer, material, slicer).metrics["support_column_mm3"] < column / 4


def _plate_with_holes(holes, size=(60, 20), thickness=5.0, res=0.1):
    """A plate with round vertical holes [(x, y, diameter)], built from thin boxes (no triangulator needed)."""
    xs = np.arange(-size[0] / 2, size[0] / 2, res) + res / 2
    boxes = []
    for y in np.arange(-size[1] / 2, size[1] / 2, res) + res / 2:
        row = np.ones(len(xs), bool)
        for hx, hy, d in holes:
            row &= (xs - hx) ** 2 + (y - hy) ** 2 > (d / 2) ** 2
        edges = np.flatnonzero(np.diff(np.concatenate([[0], row.astype(int), [0]])))
        for a, b in zip(edges[::2], edges[1::2]):
            x0, x1 = xs[a] - res / 2, xs[b - 1] + res / 2
            box = trimesh.creation.box((x1 - x0, res, thickness))
            box.apply_translation(((x0 + x1) / 2, y, thickness / 2))
            boxes.append(box)
    return trimesh.util.concatenate(boxes)


def test_holes_that_close_up_and_holes_that_need_a_fit_allowance(tmp_path, profiles):
    printer, material, slicer = profiles
    _plate_with_holes([(-20, 0, 0.8), (-5, 0, 3.0), (15, 0, 12.0)]).export(tmp_path / "holes.stl")
    ctx = run_checks(load_model(tmp_path / "holes.stl"), printer, material, slicer)
    assert sorted(round(d) for d in ctx.metrics["holes"]) == [1, 3, 12]
    assert not [f for f in ctx.findings if f.check == "check_error"]       # many bodies, no extra packages needed
    closes = [f for f in ctx.findings if f.check == "small_hole"]
    fit = [f for f in ctx.findings if f.check == "hole_fit"]
    assert len(closes) == 1 and closes[0].severity == 3
    assert len(fit) == 1 and "3.15 mm" in fit[0].fix                       # typical FDM allowance; 12 mm left alone
    learned = {**material, "hole_offset_mm": -0.3}                         # what this printer really does
    ctx = run_checks(load_model(tmp_path / "holes.stl"), printer, learned, slicer)
    fit = next(f for f in ctx.findings if f.check == "hole_fit")
    assert "3.30 mm" in fit.fix and "your prints" in fit.detail


def test_sealed_cavity_is_a_resin_trap_only_on_resin_printers(tmp_path, profiles):
    printer, material, slicer = profiles
    outer = trimesh.creation.box((20, 20, 20))
    inner = trimesh.creation.box((10, 10, 10))
    inner.invert()                                                          # a hollow inside
    hollow = trimesh.util.concatenate([outer, inner])
    hollow.apply_translation((0, 0, 10))
    hollow.export(tmp_path / "hollow.stl")
    fdm = run_checks(load_model(tmp_path / "hollow.stl"), printer, material, slicer)
    assert not [f for f in fdm.findings if f.check == "resin_trap"]
    resin_printer, resin, _ = load_profiles(CFG, "generic_resin", "resin")
    sla = run_checks(load_model(tmp_path / "hollow.stl"), resin_printer, resin, slicer)
    trap = [f for f in sla.findings if f.check == "resin_trap"]
    assert len(trap) == 1 and 800 <= trap[0].data["volume_mm3"] <= 1200
