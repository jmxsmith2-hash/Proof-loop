import dataclasses
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


def test_push_depth_and_per_part_heights():
    sf = load(plain(BUCKET))
    deeper = analyze(sf, STAGE2, LoopSettings(sweep=SweepSettings(push_offset=25)))
    assert deeper.plan.part_target == {"93": 145.0, "104": 2.0}  # 170 - 25; the short part stays at the floor
    mine = analyze(sf, STAGE2, LoopSettings(sweep=SweepSettings(part_push_z={"93": 60.0, "104": 3.0})))
    assert mine.plan.part_target == {"93": 60.0, "104": 3.0}
    assert mine.plan.part_custom == {"93", "104"}
    lanes = {ln.x: ln.tiers for ln in mine.plan.passes[0].lanes}
    assert lanes[130.0] == [60.0, 3.0]
    assert lanes[220.0] == [60.0]  # empty lanes follow the plate's highest push
    assert "G1 Z60.00" in end_block(
        process(sf, STAGE2, LoopSettings(sweep=SweepSettings(part_push_z={"93": 60.0}))).gcode
    )


def test_per_part_height_can_never_go_below_the_floor_or_above_the_part():
    sf = load(plain(BUCKET))
    low = analyze(sf, STAGE2, LoopSettings(sweep=SweepSettings(floor_z=2.5, part_push_z={"104": 0.5})))
    assert low.plan.part_target["104"] == 2.5 and any("below your sweep floor" in w for w in low.warnings)
    high = analyze(sf, STAGE2, LoopSettings(sweep=SweepSettings(part_push_z={"104": 9.0})))
    assert not high.ok and "pass over it" in high.problems[0]
    with pytest.raises(ProcessError):
        process(sf, STAGE2, LoopSettings(sweep=SweepSettings(part_push_z={"104": 9.0})))


def test_first_sweep_safety_pause_only_in_loop_one():
    g = threemf.loads(run(loops=3, pause_first_sweep=True).data).gcode
    ends = [m.start() for m in re.finditer("; >>> CLEARLOOP END", g)]
    assert len(ends) == 3
    first, rest = g[ends[0] : ends[1]], g[ends[1] :]
    assert "SAFETY PAUSE" in first and "M104 S6" in first
    assert first.index("M104 S6") < first.index("M400 U1") < first.index("; -- sweep pass 1")
    assert "SAFETY PAUSE" not in rest
    assert "SAFETY PAUSE" not in threemf.loads(run(loops=2).data).gcode
    a = analyze(load(plain(SMALL)), STAGE2, LoopSettings(loops=2, pause_first_sweep=True))
    assert "first_pause" in [s["key"] for s in a.steps]


def test_safety_pause_is_automatic_until_a_loop_has_swept(tmp_path):
    from clearloop.api import make_loop_file
    from clearloop.store import Store

    r, profile, st = make_loop_file(plain(SMALL), "small.gcode.3mf", loops=2, data_dir=tmp_path)
    assert st.pause_first_sweep and r.filename == "FL_S2_small.gcode.3mf" and st.loops == 2
    Store(tmp_path / "clearloop.db").add_production("p2s", "FL_S2_small", 1, 2, 10.0)
    _, _, st = make_loop_file(plain(SMALL), "small.gcode.3mf", data_dir=tmp_path)
    assert not st.pause_first_sweep


def test_printer_found_by_name_when_the_slicer_left_out_its_id():
    """Bambu Studio's and Orca's command line write no printer_model_id; the name is still there."""
    from clearloop import printers

    sf = load(make_3mf(plate_gcode(SMALL), model_id=""))
    assert sf.info.printer_model == "Bambu Lab P2S" and sf.info.printer_model_id == "N7"
    assert printers.model_id_for("Bambu Lab H2D") == "O1D" and printers.model_id_for("Prusa MK4") == ""


def _with_layer_changes(g: str) -> str:
    return re.sub(r"^(G1 Z[\d.]+ F600)$", r"; CHANGE_LAYER\n\1", g, flags=re.M)


def test_bambu_studio_2_object_ids():
    """Bambu Studio 2 marks each object's moves with '; OBJECT_ID: N' and no stop label."""
    g = _with_layer_changes(plate_gcode(SMALL))
    g = re.sub(r"^; start printing object, unique label id: (\d+)$", r"; OBJECT_ID: \1", g, flags=re.M)
    g = re.sub(r"^; stop printing object.*\n", "", g, flags=re.M)
    # a timelapse shot inside the object's moves (it extrudes at the chute), then the prime tower
    g = g.replace(
        "; OBJECT_ID: 1\n",
        "; OBJECT_ID: 1\n; SKIPPABLE_START\nG1 X250 Y250 F30000\nG1 E2\n; SKIPPABLE_END\n",
        1,
    )
    g = g.replace(
        "; CHANGE_LAYER\n", "; FEATURE: Prime tower\nG1 X230 Y230\nG1 X240 Y240 E3\n; CHANGE_LAYER\n"
    )
    parts = {p.label: p for p in read_parts(g)}
    assert set(parts) == {"1", "2"}
    assert (parts["1"].xmin, parts["1"].xmax, parts["1"].height) == (100.0, 140.0, 12.0)
    assert (parts["2"].xmax, parts["2"].ymax, parts["2"].height) == (190.0, 80.0, 4.0)
    assert process(load(make_3mf(g, SMALL)), STAGE2, LoopSettings()).analysis.parts


def test_orcaslicer_without_the_machine_markers():
    """OrcaSlicer leaves out MACHINE_START_GCODE_END / MACHINE_END_GCODE_START."""
    g = _with_layer_changes(plate_gcode(SMALL))
    g = g.replace("; MACHINE_START_GCODE_END\n", "").replace(
        "; MACHINE_END_GCODE_START\n", "; FEATURE: Custom\n; filament end gcode \n"
    )
    g = g.replace(";===== 2026/05/18 =====", ";===== Based on official 2026/04/21 start gcode =====")
    lay = threemf.layout(g)
    assert lay.ok and lay.lines[lay.start_gcode_end].startswith("; CHANGE_LAYER")
    assert lay.lines[lay.end_gcode_start].startswith("; filament end gcode")
    orca = process(load(make_3mf(g, SMALL)), STAGE2, LoopSettings())
    bambu = run()
    assert [p.as_dict() for p in orca.analysis.parts] == [p.as_dict() for p in bambu.analysis.parts]
    assert end_block(orca.gcode) == end_block(bambu.gcode)
    start = "\n".join(orca.gcode.splitlines()[:80])
    assert "Based on official 2026/04/21 start gcode =====\n; >>> CLEARLOOP START" in start
    # a profile saved with the Bambu-only date anchor still finds OrcaSlicer's line
    old = dataclasses.replace(STAGE2, start_anchor=r"^;=+ \d{4}/\d{2}/\d{2} =+$")
    assert process(load(make_3mf(g, SMALL)), old, LoopSettings()).gcode == orca.gcode
