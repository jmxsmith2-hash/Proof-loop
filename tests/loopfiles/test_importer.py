import pytest

from proofloop.loopfiles import threemf
from proofloop.loopfiles.gcode.processor import process
from proofloop.loopfiles.gcode.routines import LoopSettings
from proofloop.loopfiles.importer import ImportError_, import_profile
from proofloop.loopfiles.profiles import ProfileStore
from tests.plates import SMALL, farmloop_3mf, make_3mf, plain, plate_gcode

BUILTIN = ProfileStore().get("p2s-stage2-farmboard")


def load(b, name):
    return threemf.loads(b, name=name)


def test_stage2_import_recovers_the_vendor_blocks_exactly():
    r = import_profile(load(plain(), "p.gcode.3mf"), load(farmloop_3mf(), "FL_S2_p.gcode.3mf"), "My P2S")
    p = r.profile
    for k in ("start_block", "door_open", "bend", "safety", "door_close"):
        assert getattr(p, k) == getattr(BUILTIN, k), k
    assert p.sweep == BUILTIN.sweep
    assert p.sweep.lane_pitch == 30
    assert p.stage == 2 and p.cooldown_repeat == 60
    assert p.start_anchor == BUILTIN.start_anchor
    assert any("natural-cooldown" in d for d in r.dropped)
    assert "@fl:" not in p.door_close


def test_stage1_import_keeps_the_kits_bend_moves():
    r = import_profile(
        load(plain(), "p"), load(farmloop_3mf(stage=1, natural_cooldown=False), "FL_S1_p"), None
    )
    p = r.profile
    assert p.stage == 1 and not p.door_open and not p.start_block
    assert p.bend == "G1 Z250 F3000\nM400\nG4 S3\nG1 Z200 F3000\n"
    # and it is used when making a loop file
    g = process(load(plain(), "p.gcode.3mf"), p, LoopSettings()).gcode
    assert p.bend in g


def test_import_refuses_mismatched_printers_and_unknown_layouts():
    other = make_3mf(plate_gcode(SMALL), SMALL, model_id="C12")
    with pytest.raises(ImportError_, match="different printers"):
        import_profile(load(other, "a"), load(farmloop_3mf(), "b"))
    from tests.plates import farmloop_version

    text = (
        farmloop_version(plate_gcode(SMALL))
        .replace("PUSH SECTION", "PUSH")
        .replace("@fl:section:start", "@fl:x")
    )
    broken = make_3mf(text, SMALL)
    with pytest.raises(ImportError_, match="sweep section"):
        import_profile(load(plain(), "a"), load(broken, "b"))


def test_saved_profile_round_trips(tmp_path):
    store = ProfileStore(tmp_path)
    p = import_profile(load(plain(), "a"), load(farmloop_3mf(), "b"), "Shop P2S").profile
    store.save(p)
    assert store.get("shop-p2s").to_json() == p.to_json()
    with pytest.raises(ValueError):
        p.id = "p2s-stage2-farmboard"
        store.save(p)
