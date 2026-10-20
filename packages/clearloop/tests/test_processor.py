import hashlib
import io
import re
import zipfile

import pytest
from helpers import box_gcode, make_3mf

from clearloop import manifest, threemf
from clearloop.gcode.heightmap import build_heightmap
from clearloop.gcode.processor import ProcessOptions, UnsupportedMaterial, process
from clearloop.gcode.routines import LoopSettings, override_bed_temp, strip_blocks
from clearloop.gcode.sweep import SweepSettings, plan_sweep
from clearloop.profiles import combine_materials, printer_profile
from clearloop.protocol import DEFAULT_BASE, Marker

TALL_AND_SHORT = [(40, 40, 80, 80, 30.0), (150, 150, 170, 170, 4.0)]


def run(boxes=TALL_AND_SHORT, filaments=("PLA",), model="p1s", chamber=None, **loop):
    copies = loop.pop("copies", 1)
    secret = loop.pop("secret", None)
    bed_temp = loop.pop("bed_temp", None)
    sliced = threemf.loads(make_3mf(box_gcode(boxes), filaments=filaments, chamber=chamber))
    opts = ProcessOptions(
        printer=printer_profile(model),
        loop=LoopSettings(**loop),
        copies=copies,
        secret=secret,
        bed_temp=bed_temp,
    )
    return process(sliced, opts)


def markers(gcode):
    return [Marker(int(m) - DEFAULT_BASE) for m in re.findall(r"^M73 L(9\d{3})$", gcode, re.M)]


def test_heightmap_finds_each_part_and_skips_purge_line():
    hm = build_heightmap(box_gcode(TALL_AND_SHORT), 256, 256)
    objs = hm.objects()
    assert len(objs) == 2  # the purge line in the start G-code is not an object
    assert objs[0].height == pytest.approx(30.0)
    assert objs[1].height == pytest.approx(4.0)
    assert objs[0].xmin == pytest.approx(40, abs=2) and objs[0].xmax == pytest.approx(80, abs=2)


def test_heightmap_without_start_marker_still_ignores_nothing_on_plate():
    hm = build_heightmap(box_gcode([(50, 50, 60, 60, 2.0)], start_marker=False, purge=False), 256, 256)
    assert len(hm.objects()) == 1


def test_short_parts_get_their_own_low_push():
    r = run()
    by_target = {p.target: p for p in r.plan.passes}
    tall, short = by_target[0], by_target[1]
    # A single "tallest minus 10 mm" push would be at 20 mm and miss the 4 mm part entirely.
    assert short.push_z < 4.0
    assert tall.push_z < 30.0
    assert tall.travel_z > 30.0


def test_front_parts_are_cleared_first_and_travel_clears_standing_parts():
    boxes = [(100, 180, 140, 220, 10.0), (100, 20, 140, 60, 25.0)]  # tall part in front, short behind
    r = run(boxes)
    order = [p.target for p in r.plan.passes]
    front = min(range(2), key=lambda i: r.objects[i].ymin)
    assert order[0] == front
    first = r.plan.passes[0]
    assert first.travel_z >= 25.0 + 5  # travel clears the tallest part still standing


def test_tiny_part_warns_but_never_silently():
    r = run([(60, 60, 70, 70, 0.8)])
    assert any("sweep floor" in w for w in r.warnings)


def test_material_aware_natural_cooldown():
    asa_enclosed = run(filaments=("ASA",), model="p1s", door_opener=True)
    assert asa_enclosed.manifest["natural_cooldown_min"] == 20
    g = asa_enclosed.gcode
    # Fans off and door shut *before* the door-open marker.
    assert g.index("warp-safe natural cooldown") < g.index("CLEARLOOP_MARKER DOOR_OPEN")

    assert run(filaments=("PLA",), model="p1s").manifest["natural_cooldown_min"] == 0
    assert run(filaments=("ASA",), model="a1").manifest["natural_cooldown_min"] == 0  # open frame
    assert run(filaments=("PETG",), model="x1e", chamber=45).manifest["natural_cooldown_min"] >= 20


def test_release_temp_uses_coolest_material():
    assert combine_materials(["PLA", "ABS"]).release_temp == 30
    assert combine_materials(["ABS", "PC"]).natural_cooldown_min == 30


def test_unsupported_material_is_refused():
    with pytest.raises(UnsupportedMaterial):
        run(filaments=("TPU",))


def test_bed_heater_is_off_after_cooldown_wait():
    g = run().gcode
    wait = g.index("M190 R30")
    assert g.index("M140 S0", wait) < g.index("CLEARLOOP_MARKER EJECT")


def test_bed_temperature_written_exactly():
    r = run(bed_temp=65)
    temps = re.findall(r"^M1(?:40|90) S(\d+(?:\.\d+)?)", r.gcode, re.M)
    assert "65" in temps
    assert all(t in ("65", "0") for t in temps)
    assert override_bed_temp("M140 S0\nM190 S55 ; wait\n", 70) == "M140 S0\nM190 S70 ; wait\n"


def test_3mf_checksum_updated_and_other_entries_kept():
    r = run()
    z = zipfile.ZipFile(io.BytesIO(r.data))
    gcode = z.read("Metadata/plate_1.gcode")
    assert z.read("Metadata/plate_1.gcode.md5").decode() == hashlib.md5(gcode).hexdigest().upper()
    assert z.read("Metadata/plate_1.png") == b"\x89PNG"
    assert "Metadata/clearloop.json" in z.namelist()


def test_processing_is_idempotent():
    first = run()
    again = process(threemf.loads(first.data), ProcessOptions(printer=printer_profile("p1s")))
    body = lambda g: g.split("\n", 1)[1]  # noqa: E731 - ignore the manifest line (timestamp)
    assert body(again.gcode) == body(first.gcode)


def test_marker_sequence_board_mode():
    r = run(eject_mode="board", door_opener=True)
    assert markers(r.gcode) == [Marker.JOB_START, Marker.DOOR_OPEN, Marker.EJECT, Marker.SWEEP_DONE]
    assert "G1 Z250 F900 ; present the plate to the bender" in r.gcode


def test_marker_sequence_multi_copy():
    r = run(eject_mode="board", copies=3)
    seq = markers(r.gcode)
    assert seq.count(Marker.JOB_START) == 3
    assert seq.count(Marker.LOOP_END) == 2
    assert seq.count(Marker.EJECT) == 3
    # Stripping the ClearLoop blocks gives back one clean copy.
    stripped = strip_blocks(r.gcode)
    assert stripped.count("; MACHINE_START_GCODE_END") == 1


def test_gcode_mode_has_no_bend_and_custom_snippet_is_inserted():
    g = run(eject_mode="gcode").gcode
    assert "present the plate to the bender" not in g
    g = run(eject_mode="custom", custom_eject_gcode="M400\nG1 Z240 ; press switch").gcode
    assert "G1 Z240 ; press switch" in g


def test_job_start_comes_before_homing():
    g = run().gcode
    assert g.index("CLEARLOOP_MARKER JOB_START") < g.index("G28")


def test_manifest_survives_rename_and_detects_tampering():
    r = run(secret="s3cret")
    v = manifest.verify(r.gcode, "s3cret")
    assert v.ok and v.signature == "valid"
    # The manifest travels inside the G-code, so the file name is irrelevant.
    renamed = threemf.loads(r.data, name="totally-different-name.gcode.3mf")
    assert manifest.verify(renamed.gcode, "s3cret").ok
    tampered = r.gcode.replace("M190 R30", "M190 R60")
    assert not manifest.verify(tampered, "s3cret").gcode_matches
    assert manifest.verify(r.gcode, "wrong").signature == "invalid"


def test_plain_gcode_container():
    sliced = threemf.loads(box_gcode([(50, 50, 70, 70, 10)]).encode() + b"; filament_type = PETG\n")
    assert sliced.container == "gcode"
    assert sliced.info.filaments == ["PETG"]
    r = process(sliced, ProcessOptions(printer=printer_profile("a1")))
    assert r.data.decode().startswith(manifest.LINE_PREFIX)


def test_wrong_printer_warns():
    sliced = threemf.loads(make_3mf(box_gcode(TALL_AND_SHORT), printer_model="Bambu Lab A1 mini"))
    r = process(sliced, ProcessOptions(printer=printer_profile("p1s")))
    assert any("sliced for" in w for w in r.warnings)


def test_final_wipe_covers_the_plate():
    prof = printer_profile("p1s")
    plan = plan_sweep([], prof, SweepSettings(final_wipe=True, wipe_spacing=32))
    assert len(plan.passes) == 8
    assert all(p.push_z == prof.min_sweep_z for p in plan.passes)
