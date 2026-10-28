"""CAD -> slicer -> G-code, with a stand-in for Bambu Studio's command line and a small preset tree."""

import json
from pathlib import Path

import pytest
import trimesh
from typer.testing import CliRunner

from synthux.cli import app
from synthux.core.config import ConfigError, load_settings
from synthux.core.db import Database
from synthux.physical.prepare import prepare
from synthux.physical.slicer import PresetLibrary, find_slicer

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "config"
FAKE = REPO / "tests" / "fixtures" / "fake_slicer.py"
MACHINE = "Bambu Lab P2S 0.4 nozzle"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def profiles(tmp_path, monkeypatch):
    """A preset tree laid out like Bambu Studio's resources/profiles (inheritance and templates)."""
    root = tmp_path / "profiles" / "BBL"
    _write(root / "machine" / "fdm_machine_common.json",
           {"name": "fdm_machine_common", "printable_area": ["0x0", "256x0", "256x256", "0x256"],
            "machine_start_gcode": "; generic start"})
    _write(root / "machine" / f"{MACHINE}.json",
           {"name": MACHINE, "inherits": "fdm_machine_common", "instantiation": "true", "nozzle_diameter": ["0.4"],
            "printer_model": "Bambu Lab P2S"})
    _write(root / "machine" / f"{MACHINE} template machine_start_gcode.json",  # the P2S template's header
           {"name": f"{MACHINE} template machine_start_gcode",
            "machine_start_gcode": ";M1002 set_flag extrude_cali_flag=1\n\n;======== P2S start gcode==========\n"
                                   ";===== 2026/05/18 =====\n\n  M140 S[bed_temperature_initial_layer_single]\n"
                                   "  M400\nG28\nM109 S[nozzle_temperature_initial_layer]"})
    _write(root / "machine" / f"{MACHINE} template machine_end_gcode.json",
           {"name": f"{MACHINE} template machine_end_gcode",
            "machine_end_gcode": ";======== P2S end gcode ==========\nM400\nG92 E0\nM140 S0\nM106 S0\nM104 S0\n"
                                 "M400\nM18"})
    _write(root / "process" / "fdm_process_common.json", {"name": "fdm_process_common", "enable_support": "0"})
    for h, name in ((0.2, "0.20mm Standard @BBL P2S"), (0.12, "0.12mm High Quality @BBL P2S"),
                    (0.2, "0.20mm Strength @BBL P2S")):
        _write(root / "process" / f"{name}.json",
               {"name": name, "inherits": "fdm_process_common", "instantiation": "true", "layer_height": str(h),
                "compatible_printers": [MACHINE]})
    for name, kind, temp in (("Bambu PLA Basic @BBL P2S", "PLA", "220"), ("Generic PLA @BBL P2S", "PLA", "215"),
                             ("Bambu PETG HF @BBL P2S", "PETG", "250")):
        _write(root / "filament" / f"{name}.json",
               {"name": name, "instantiation": "true", "filament_type": [kind], "nozzle_temperature": [temp],
                "hot_plate_temp": ["55"], "compatible_printers": [MACHINE]})
    monkeypatch.setenv("SYNTHUX_SLICER_PROFILES", str(tmp_path / "profiles"))
    monkeypatch.setenv("SYNTHUX_SLICER", str(FAKE))
    return tmp_path / "profiles"


def test_presets_are_found_and_flattened(profiles):
    lib = PresetLibrary([profiles])
    assert lib.machine("Bambu Lab P2S", 0.4) == MACHINE
    assert lib.process(MACHINE, 0.2) == "0.20mm Standard @BBL P2S"          # 'Standard' before 'Strength'
    assert lib.process(MACHINE, 0.12) == "0.12mm High Quality @BBL P2S"
    assert lib.filament(MACHINE, "PLA") == "Bambu PLA Basic @BBL P2S"       # Bambu's own Basic first
    flat = lib.flat(MACHINE)
    assert flat["machine_start_gcode"].startswith(";M1002") and flat["printable_area"][1] == "256x0"
    assert "inherits" not in flat
    with pytest.raises(ConfigError, match="no ABS filament"):
        lib.filament(MACHINE, "ABS")
    found = find_slicer()
    assert found and found.exe == FAKE and found.profile_dirs == [profiles.resolve()]


@pytest.fixture()
def setup(tmp_path):
    settings = load_settings(CFG).model_copy(update={"data_dir": tmp_path / "data"})
    return settings, Database(tmp_path / "data" / "s.sqlite")


def test_overhang_without_support_is_caught_and_support_fixes_it(profiles, setup, tmp_path):
    settings, db = setup
    stl = REPO / "examples" / "cad" / "phone_stand_v1.stl"
    off = prepare(settings, db, stl, out=tmp_path / "off", personas=2)
    assert not off["ready"]
    assert [f["title"] for f in off["slicer_findings"]] == ["No support under the overhang"]
    assert off["presets"] == {"machine": MACHINE, "process": "0.20mm Standard @BBL P2S",
                              "filament": "Bambu PLA Basic @BBL P2S", "overrides": {}}
    assert Path(off["report"]).is_file() and off["grams"] > 0
    on = prepare(settings, db, stl, support=True, out=tmp_path / "on", personas=2)
    assert not on["slicer_findings"] and "support" in on["gcode"]["metrics"]["minutes_by_feature"]


def test_a_wall_too_thin_for_the_nozzle_is_reported_as_left_out(profiles, setup, tmp_path):
    settings, db = setup
    base = trimesh.creation.box((30, 20, 3))
    base.apply_translation((0, 0, 1.5))
    fin = trimesh.creation.box((0.3, 16, 12))
    fin.apply_translation((0, 0, 9))
    stl = tmp_path / "fin.stl"
    trimesh.util.concatenate([base, fin]).export(stl)
    r = prepare(settings, db, stl, out=tmp_path / "fin", personas=2)
    dropped = [f for f in r["slicer_findings"] if f["check"] == "slicer_dropped"]
    assert dropped and dropped[0]["severity"] == 4 and not r["ready"]


def test_prepare_can_make_a_clearloop_loop_file(profiles, setup, tmp_path):
    pytest.importorskip("clearloop")
    settings, db = setup
    r = prepare(settings, db, REPO / "examples" / "cad" / "block_control.stl", loops=2, out=tmp_path / "l",
                personas=2)
    assert r["loop"]["metrics"]["loop_file"]["loops"] == 2 and Path(r["loop"]["path"]).name.startswith("FL_S2_")


def test_cli_without_a_slicer_says_how_to_get_one(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNTHUX_SLICER", raising=False)
    monkeypatch.setattr("synthux.physical.slicer._candidates", lambda: [])
    out = CliRunner().invoke(app, ["physical", "prepare", str(REPO / "examples" / "cad" / "block_control.stl"),
                                   "-c", str(CFG), "-o", str(tmp_path / "x")])
    assert out.exit_code == 2 and "no Bambu Studio or OrcaSlicer found" in out.output


def test_app_window_slices_when_asked(profiles, tmp_path):
    import http.client
    import threading
    import time

    from synthux.app.server import serve

    settings = load_settings(CFG).model_copy(update={"data_dir": tmp_path / "data"})
    db = Database(settings.db_path)
    server, the_app = serve(settings, db, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        def req(method, path, body=None):
            c = http.client.HTTPConnection("127.0.0.1", the_app.port, timeout=60)
            c.request(method, path, body=body, headers={"Host": f"127.0.0.1:{the_app.port}", "X-Synthux": "1"})
            r = c.getresponse()
            return r.status, r.read()

        assert b"Slice it with Bambu Studio too" in req("GET", "/")[1]
        body = (REPO / "examples" / "cad" / "block_control.stl").read_bytes()
        status, data = req("POST", "/api/check?name=block.stl&slice=1", body)
        assert status == 201
        jid = json.loads(data)["id"]
        for _ in range(300):
            job = next(j for j in json.loads(req("GET", "/api/jobs")[1]) if j["id"] == jid)
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        assert job["status"] == "done", job.get("error")
        assert job["title"] == "block.stl: ready to print" and job["summary"][0].startswith("Ready to print")
    finally:
        server.shutdown()
        db.close()


def test_fix_applies_the_settings_and_slices_again(profiles, setup, tmp_path):
    settings, db = setup
    r = prepare(settings, db, REPO / "examples" / "cad" / "phone_stand_v1.stl", fix=True, out=tmp_path / "fix",
                personas=2)
    assert r["rounds"] and r["rounds"][0]["blocking_after"] < r["rounds"][0]["blocking_before"]
    applied = {s["key"]: s["value"] for s in r["rounds"][0]["applied"]}
    assert applied["enable_support"] == "1" and applied["support_type"] == "tree(auto)"
    assert not r["slicer_findings"] and r["presets"]["overrides"]["process"]["enable_support"] == "1"
    flat = json.loads((tmp_path / "fix" / "presets" / "2" / "process.json").read_text(encoding="utf-8"))
    assert flat["enable_support"] == "1"
