from fixtures import BUCKET, farmloop_3mf, make_3mf, plain, plate_gcode

from clearloop import threemf
from clearloop.gcode.processor import process
from clearloop.gcode.routines import LoopSettings
from clearloop.lint import lint
from clearloop.profiles import ProfileStore


def titles(data, name="FL_S2_x.gcode.3mf"):
    return {f.title: f.level for f in lint(threemf.loads(data, name=name))}


def test_farmloop_style_file_is_flagged():
    t = titles(farmloop_3mf(BUCKET))
    assert t["Loop 1: 120 min wait before printing, heaters on"] == "danger"
    assert t["Parts the sweep passes over"] == "danger"


def test_clearloop_output_passes():
    sf = threemf.loads(plain(BUCKET), name="x.gcode.3mf")
    r = process(sf, ProfileStore().get("p2s-stage2-farmboard"), LoopSettings(loops=2))
    t = titles(r.data, r.filename)
    assert t == {"Every part is reached by a sweep pass": "ok", "No known problems found": "ok"}


def test_m190r_stray_code_and_motion_after_m18():
    g = plate_gcode(BUCKET)
    bad = g.replace("M140 S0 ; turn off bed", "M190 R30").replace(
        "; EXECUTABLE_BLOCK_END\n", "; EXECUTABLE_BLOCK_END\nG1 X10\n"
    )
    bad = bad.replace("M18\n", "M18\nG1 Z20 F600\n")
    t = titles(make_3mf(bad, BUCKET))
    assert t["Uses M190 R"] == "danger"
    assert t["G-code after the last executable block"] == "warning"
    assert t["Loop 1: moves after steppers were switched off"] == "danger"


def test_checksum_mismatch():
    import io
    import zipfile

    buf = io.BytesIO()
    src = zipfile.ZipFile(io.BytesIO(plain()))
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            z.writestr(n, b"0000" if n.endswith(".md5") else src.read(n))
    assert titles(buf.getvalue())["Checksum doesn't match"] == "danger"
