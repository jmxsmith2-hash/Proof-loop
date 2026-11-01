"""Slicing in ProofLoop: a model or a Bambu Studio project goes in, a sliced plate comes out."""

import io
import json
import zipfile

import pytest
import trimesh

from proofloop.check import slicer as slicer_mod
from proofloop.loopfiles import slicing, threemf
from tests.check.test_prepare import CFG, MACHINE, _write, profiles  # noqa: F401  (the preset tree fixture)

PROCESS, PLA = "0.20mm Standard @BBL P2S", "Bambu PLA Basic @BBL P2S"


def _cube() -> bytes:
    buf = io.BytesIO()
    trimesh.creation.box((10, 10, 10)).export(buf, file_type="stl")
    return buf.getvalue()


def _project(flow: str = "Standard", sliced: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/project_settings.config", json.dumps(
            {"printer_model": "Bambu Lab P2S", "nozzle_diameter": ["0.4"], "nozzle_volume_type": [flow],
             "layer_height": "0.2", "filament_type": ["PLA"]}))
        if sliced:
            zf.writestr("Metadata/plate_1.gcode", "; old slice")
    return buf.getvalue()


@pytest.fixture()
def lib(profiles, tmp_path, monkeypatch):  # noqa: F811
    root = profiles / "BBL"
    _write(root / "filament" / "Bambu PLA Basic @BBL P2S.json",  # as the real presets: the RFID tag's id
           {"name": PLA, "instantiation": "true", "filament_type": ["PLA"], "filament_id": "GFA00",
            "nozzle_temperature": ["220"], "hot_plate_temp": ["55"], "compatible_printers": [MACHINE]})
    mine = tmp_path / "appdata" / "BambuStudio" / "user" / "123"
    _write(mine / "filament" / "My PLA.json",  # saved in Bambu Studio: only what differs from its base
           {"name": "My PLA", "inherits": "Generic PLA @BBL P2S", "instantiation": "true",
            "nozzle_temperature": ["210"], "from": "User"})
    monkeypatch.setattr(slicer_mod, "user_dirs_for", lambda kind: [mine])
    return slicing.slicer_and_presets()


def _gcode(data: bytes, plate: int = 1) -> str:
    return zipfile.ZipFile(io.BytesIO(data)).read(f"Metadata/plate_{plate}.gcode").decode()


def test_what_kind_of_file_it_is():
    assert slicing.kind_of("part.STL", b"") == "mesh"
    assert slicing.kind_of("part.step", b"") == "mesh"
    assert slicing.kind_of("box.3mf", _project()) == "project"
    assert slicing.kind_of("box.gcode.3mf", _project(sliced=True)) == "plate"
    assert slicing.kind_of("notes.txt", b"hi") == ""
    assert slicing.kind_of("broken.3mf", b"not a zip") == ""


def test_the_presets_for_a_p2s_include_your_own(lib):
    found, presets = lib
    opts = slicing.options(presets)
    assert [m["name"] for m in opts["machines"]] == [MACHINE]
    assert opts["processes"][MACHINE][0] == "0.12mm High Quality @BBL P2S"  # finest first
    fils = {f["name"]: f for f in opts["filaments"][MACHINE]}
    assert fils["My PLA"]["mine"] and fils["My PLA"]["type"] == "PLA"  # inherited from its base
    assert opts["filaments"][MACHINE][0]["name"] == "My PLA"  # yours first


def test_an_ams_tray_finds_its_filament_preset(lib):
    _, presets = lib
    opts = slicing.options(presets)
    pick = lambda tray: slicing.filament_for_tray(presets, MACHINE, tray, opts)  # noqa: E731
    assert pick({"filament_id": "GFA00", "type": "PLA"}) == PLA
    assert pick({"filament_id": "GFX99", "type": "PETG"}) == "Bambu PETG HF @BBL P2S"
    assert pick({"filament_id": "", "type": "PLA"}) == PLA  # Bambu's own before generic or yours
    assert pick({"type": "TPU"}) == ""


def test_a_model_is_sliced_for_the_printers_nozzle_and_keeps_the_sweep_strip_clear(lib, tmp_path):
    found, presets = lib
    choice = slicing.Choice(machine=MACHINE, process=PROCESS, filament="My PLA", nozzle_flow="Standard",
                            support=True, clear_behind_y=236, plate="Textured PEI Plate")
    data, name, notes = slicing.slice_upload(_cube(), "cube.stl", choice, tmp_path / "w", found=found, lib=presets)
    assert name == "cube.gcode.3mf" and notes == []
    g = _gcode(data)
    assert "; enable_support = 1" in g and "; nozzle_volume_type = Standard" in g
    assert "; bed_exclude_area = 0x236,256x236,256x256,0x256" in g and "; curr_bed_type = Textured PEI Plate" in g
    assert "My PLA" in g and threemf.sliced_plates(data) == [1]


def test_fill_the_plate_keeps_the_full_plate_and_copies_overflow_onto_more(lib, tmp_path):
    found, presets = lib
    fill = slicing.Choice(machine=MACHINE, process=PROCESS, filament=PLA, fill=True)
    data, _, notes = slicing.slice_upload(_cube(), "cube.stl", fill, tmp_path / "a", found=found, lib=presets)
    assert threemf.sliced_plates(data) == [1] and notes == ["25 copies fill the plate"]
    many = slicing.Choice(machine=MACHINE, process=PROCESS, filament=PLA, copies=30)
    data, _, notes = slicing.slice_upload(_cube(), "cube.stl", many, tmp_path / "b", found=found, lib=presets)
    assert threemf.sliced_plates(data) == [1, 2] and "the slicer made 2 plates" in notes[0]


def test_wrong_choices_say_why(lib, tmp_path):
    found, presets = lib
    for choice, words in ((slicing.Choice(machine="Bambu Lab X1C 0.4 nozzle"), "isn't a Bambu Lab P2S preset"),
                          (slicing.Choice(machine=MACHINE, process=PROCESS, filament="Nope"), "doesn't fit"),
                          (slicing.Choice(machine=MACHINE, process=PROCESS, filament=PLA, nozzle_flow="High Flow"),
                           "has no High Flow nozzle")):
        with pytest.raises(slicing.SliceError, match=words):
            slicing.slice_upload(_cube(), "cube.stl", choice, tmp_path / "w", found=found, lib=presets)
    with pytest.raises(slicing.SliceError, match="already sliced"):
        slicing.slice_upload(_project(sliced=True), "p.gcode.3mf", slicing.Choice(), tmp_path, found=found, lib=presets)
    with pytest.raises(slicing.SliceError, match="copies must be"):
        slicing.Choice.from_dict({"copies": 500})
    with pytest.raises(slicing.SliceError, match="the plate must be"):
        slicing.Choice.from_dict({"plate": "Glass"})


def test_a_project_is_sliced_with_its_own_settings_and_the_printers_nozzle_flow(lib, tmp_path):
    found, presets = lib
    proj = _project("Standard", sliced=True)
    assert slicing.project_flow(proj) == "Standard"
    fixed = slicing.as_project(proj, "High Flow")
    assert slicing.project_flow(fixed) == "High Flow"
    assert "Metadata/plate_1.gcode" not in zipfile.ZipFile(io.BytesIO(fixed)).namelist()  # the old slice goes
    data, name, notes = slicing.slice_upload(_project(), "box.3mf", slicing.Choice(nozzle_flow="High Flow"),
                                             tmp_path / "w", found=found, lib=presets)
    assert name == "box.gcode.3mf" and "; nozzle_volume_type = High Flow" in _gcode(data)
    assert notes == ["sliced for a High Flow nozzle (the project was set for Standard)"]


def test_make_a_loop_file_slices_a_dropped_model(lib, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from proofloop.app.shell import Shell

    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path / "home"))
    shell = Shell(8600, config=CFG, connect=False)
    try:
        c = TestClient(shell.api, base_url="http://127.0.0.1:8600")
        window = {"X-ProofLoop": "1", "Origin": "http://127.0.0.1:8600"}
        opts = c.get("/api/slice/options", headers=window).json()
        assert opts["slicer"] and opts["machines"][0]["name"] == MACHINE and opts["printers"] == []
        model = c.post("/api/plates", files={"file": ("cube.stl", _cube())}, headers=window).json()["model"]
        assert model["kind"] == "mesh" and model["name"] == "cube.stl"
        r = c.post(f"/api/models/{model['token']}/slice", headers=window,
                   json={"model_id": "N7", "machine": MACHINE, "process": PROCESS, "filament": PLA, "copies": 2})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["token"] and out["sliced_from"]["name"] == "cube.stl" and out["slice_notes"] == []
        assert "bed_exclude_area = 0x" in _gcode(shell.monitor.uploads.raw(out["token"])[0])  # the sweep strip stays clear
        bad = c.post(f"/api/models/{model['token']}/slice", headers=window, json={"filament": "Nope"})
        assert bad.status_code == 422 and "doesn't fit" in bad.json()["detail"]
    finally:
        shell.close()
