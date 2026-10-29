import json
from pathlib import Path

import pytest
import trimesh

from proofloop.check.cad.checks import run_checks
from proofloop.check.cad.eject import copies_note, eject_settings
from proofloop.check.cad.load import Model, load_profiles

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def profiles():
    return load_profiles(REPO / "src" / "proofloop" / "data", None, None)


def run(size, profiles, use=None):
    box = trimesh.creation.box(size)
    box.apply_translation((0, 0, size[2] / 2))
    printer, material, slicer = profiles
    ctx = run_checks(Model(path=Path("p.stl"), mesh=box, file_hash="x"), printer, material, slicer,
                     {"auto_eject": True, "data_dir": "/nonexistent", **(use or {})})
    return ctx, {f.title: f for f in ctx.findings if f.check == "auto_eject"}


def test_parts_too_low_or_barely_above_the_floor(profiles):
    _, low = run((20, 20, 1.5), profiles)
    assert low["Too low for the pusher"].severity == 4
    _, barely = run((20, 20, 2.8), profiles)
    assert "Barely above the pusher's floor" in barely and "Too low for the pusher" not in barely
    _, fine = run((20, 20, 15), profiles)
    assert not fine


def test_tipping_sticking_and_tiny_parts(profiles):
    _, tower = run((10, 10, 80), profiles)
    assert tower["Tips over when pushed"].data["push_z"] == pytest.approx(70)
    _, flat = run((120, 100, 3), profiles)
    assert flat["Large flat contact may not let go"].data["contact_cm2"] == pytest.approx(120, abs=1)
    _, tiny = run((4, 4, 4), profiles)
    assert "Very small part" in tiny


def test_copies_per_plate_inside_the_lanes(profiles):
    ctx, _ = run((20, 20, 10), profiles)
    ae = ctx.metrics["auto_eject"]
    assert ae["layout"] == [9, 9] and ae["copies_per_plate"] == 81
    assert copies_note(ctx.metrics, 5.0) == "81 copies fit on one plate (9 across x 9 deep), about 405 g per loop"
    ctx, found = run((240, 60, 10), profiles)
    assert ctx.metrics["auto_eject"]["copies_per_plate"] == 0 and "Doesn't fit inside the sweep's lanes" in found


def test_settings_come_from_settings(tmp_path, profiles):
    (tmp_path / "settings.json").write_text(json.dumps({"defaults": {"floor_z": 4.0, "floor_calibrated": True,
                                                                     "bender_fitted": False}}), encoding="utf-8")
    st = eject_settings(tmp_path)
    assert st["floor_z"] == 4.0 and st["from_settings"] and not st["bender_fitted"]
    _, found = run((20, 20, 4.2), profiles, {"data_dir": str(tmp_path)})
    assert "Too low for the pusher" in found                         # 4.2 mm with a 4 mm floor
    _, flat = run((120, 100, 6), profiles, {"data_dir": str(tmp_path)})
    assert flat["Large flat contact may not let go"].severity == 2  # no bender


def test_off_unless_asked(profiles):
    box = trimesh.creation.box((20, 20, 1.5))
    printer, material, slicer = profiles
    ctx = run_checks(Model(path=Path("p.stl"), mesh=box, file_hash="x"), printer, material, slicer, {})
    assert "auto_eject" not in ctx.metrics
