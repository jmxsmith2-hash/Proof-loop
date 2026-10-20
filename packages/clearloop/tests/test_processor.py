import hashlib
import io
import re
import zipfile

import pytest
from fixtures import BUCKET, SMALL, farmloop_3mf, make_3mf, plain, plate_gcode

from clearloop import threemf
from clearloop.gcode.objects import read_parts
from clearloop.gcode.processor import ProcessError, analyze, process
from clearloop.gcode.routines import LoopSettings
from clearloop.gcode.sweep import SweepSettings
from clearloop.profiles import ProfileStore

STAGE2 = ProfileStore().get("p2s-stage2-farmboard")
STAGE1 = ProfileStore().get("p2s-sweep-only")


def load(data, name="plate.gcode.3mf"):
    return threemf.loads(data, name=name)


def run(boxes=SMALL, profile=STAGE2, **kw):
    sweep = kw.pop("sweep", SweepSettings())
    return process(load(plain(boxes)), profile, LoopSettings(sweep=sweep, **kw))


def end_block(g):
    return g[g.index("; >>> CLEARLOOP END") : g.index("; <<< CLEARLOOP END")]


def test_reads_plate_facts_from_the_file():
    sf = load(plain(BUCKET))
    i = sf.info
    assert (i.printer_model_id, i.bed_x, i.bed_y, i.printable_height) == ("N7", 256, 256, 256)
    assert i.object_heights == {"93": 170.0, "104": 5.0}
    parts = read_parts(sf.gcode, i.object_names, i.object_heights)
    assert [round(p.height, 1) for p in parts] == [5.0, 170.0]  # sorted front to back
    assert all(p.slicer_height == round(p.height, 1) for p in parts)


def test_rejects_non_sliced_files():
    with pytest.raises(threemf.PlateFileError, match="not a .gcode.3mf"):
        threemf.loads(b"G28\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(threemf.PlateFileError, match="project file"):
        threemf.loads(buf.getvalue())


def test_damaged_zip_is_a_friendly_error():
    data = plain()
    with pytest.raises(threemf.PlateFileError, match="damaged"):
        threemf.loads(data[: len(data) // 2])


def test_short_part_gets_its_own_push():
    a = analyze(load(plain(BUCKET)), STAGE2, LoopSettings())
    lanes = {ln.x: ln.tiers for ln in a.plan.passes[0].lanes}
    assert lanes[130.0] == [160.0, 2.0]  # 170 mm bucket and 5 mm clip share this lane
    assert lanes[220.0] == [160.0]  # empty lane: FarmLoop's single height, never less than vendor
    assert all(a.plan.part_push[p.label] < p.height for p in a.parts)


def test_cycle_placement_and_safety():
    g = run().gcode
    eb = end_block(g)
    # inside the executable block, right before its end marker
    assert g.index("; <<< CLEARLOOP END") < g.index("; EXECUTABLE_BLOCK_END")
    assert g[g.index("; <<< CLEARLOOP END") :].split("\n")[1] == "; EXECUTABLE_BLOCK_END"
    # after the stock end G-code, which ends with M18
    assert g.index("M18") < g.index("; >>> CLEARLOOP END")
    assert eb.split("\n")[2].startswith("M17")
    assert "M190 R" not in g
    assert eb.count(f"M190 S{30:g}") == STAGE2.cooldown_repeat
    assert eb.index("M190 S30") > eb.index("M104 S2")  # door opens, then the cooldown (FarmLoop order)


def test_farmboard_steps_are_verbatim_and_in_order():
    g = run().gcode
    eb = end_block(g)
    for blk in (STAGE2.door_open, STAGE2.bend, STAGE2.safety, STAGE2.door_close):
        assert blk in eb
    seq = re.findall(r"^M104 S([1-5])\b", g, re.M)
    assert seq == ["1", "2", "3", "4", "5"]
    # start step right after the start template's date line
    start = g.index("; >>> CLEARLOOP START")
    assert g[:start].rstrip().endswith(";===== 2026/05/18 =====")


def test_bender_removed_skips_bend_and_safety():
    g = run(bender_fitted=False).gcode
    assert re.findall(r"^M104 S([1-5])\b", g, re.M) == ["1", "2", "5"]


def test_stage1_has_no_farmboard_signals():
    r = run(profile=STAGE1)
    assert not re.search(r"^M104 S[1-5]\b", r.gcode, re.M)
    assert r.filename.startswith("FL_S1_")
    assert "Stage 1 has no door opener" in " ".join(r.analysis.notes)


def test_loops_like_farmloop_and_pause_only_between_loops():
    r = run(loops=3, wait_for_monitor=True)
    g = r.gcode
    assert g.count("; EXECUTABLE_BLOCK_START") == 3 and g.count("; HEADER_BLOCK_START") == 3
    assert len(re.findall(r"^M400 U1$", g, re.M)) == 2
    last = g[g.rindex("; >>> CLEARLOOP END") :]
    assert "M400 U1" not in last
    z = zipfile.ZipFile(io.BytesIO(r.data))
    data = z.read("Metadata/plate_1.gcode")
    assert z.read("Metadata/plate_1.gcode.md5").decode() == hashlib.md5(data).hexdigest().upper()
    assert z.read("Metadata/plate_1.png") == b"\x89PNG"


def test_natural_cooldown_runs_after_the_print_with_heaters_off():
    g = run(natural_cooldown_min=10).gcode
    eb = end_block(g)
    nat = eb.index("warp-safe natural cooldown")
    assert eb.index("M140 S0") < nat < eb.index("M104 S2")
    assert eb[nat : eb.index("M104 S2")].count("G4 S30") == 20
    # the stock fan-off lines (incl. P10, the P2S left aux fan) are reused
    assert "M106 P10 S0" in eb[nat:]
    # and nothing before the print
    assert "G4 S30" not in g[: g.index("; MACHINE_START_GCODE_END")]


def test_reprocessing_own_output_is_identical():
    first = run(loops=2)
    again = process(load(first.data, first.filename), STAGE2, LoopSettings(loops=2))
    assert again.gcode == first.gcode
    assert again.filename == first.filename


def test_refuses_farmloop_output():
    with pytest.raises(ProcessError, match="already processed by the FarmLoop app"):
        process(load(farmloop_3mf()), STAGE2, LoopSettings())


def test_refuses_wrong_printer_and_missing_labels():
    with pytest.raises(ProcessError, match="sliced for the A1"):
        analyze(load(make_3mf(plate_gcode(SMALL), SMALL, model_id="N2S")), STAGE2, LoopSettings())
    unlabelled = re.sub(r"^; (start|stop) printing object.*\n", "", plate_gcode(SMALL), flags=re.M)
    with pytest.raises(ProcessError, match="Label objects"):
        analyze(load(make_3mf(unlabelled, SMALL)), STAGE2, LoopSettings())


def test_flexible_material_is_a_problem():
    a = analyze(load(plain(filament="TPU")), STAGE2, LoopSettings())
    assert not a.ok and "rigid" in a.problems[0]


def test_part_at_the_rear_blocks():
    a = analyze(load(plain([("1", 100, 200, 140, 245, 10.0)])), STAGE2, LoopSettings())
    assert not a.ok and "Move the part forward" in a.problems[0]


def test_part_lower_than_floor_warns():
    a = analyze(
        load(plain([("1", 100, 100, 140, 140, 10.0), ("2", 170, 60, 190, 80, 1.0)])),
        STAGE2,
        LoopSettings(sweep=SweepSettings(floor_z=2.0)),
    )
    assert any("lower than your sweep floor" in w for w in a.warnings)


def test_exhaust_fan_option():
    g = run(cooldown_fan="exhaust").gcode
    assert "M106 P3 S255" in end_block(g) and "M106 P2 S255" not in end_block(g)


def test_timeline_only_states_known_durations():
    a = analyze(load(plain()), STAGE2, LoopSettings(loops=2, wait_for_monitor=True))
    by = {s["key"]: s for s in a.steps}
    assert by["cool"]["seconds"] is None and by["check"]["seconds"] is None
    assert by["bend"]["seconds"] == round(0.5 + 0.15 * 2 + 0.3 * 2 + 0.15 + 6 + 85)
