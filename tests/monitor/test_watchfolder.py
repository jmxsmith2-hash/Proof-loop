"""The watch folder: models saved into a folder are sliced and waiting in Make a loop file."""

import os
import time

import pytest

from proofloop.core.settings import SliceUsual, WatchFolder
from proofloop.loopfiles import slicing, threemf
from proofloop.monitor.service import App
from tests.check.test_prepare import MACHINE, profiles  # noqa: F401  (the preset tree and the stand-in slicer)
from tests.loopfiles.test_slicing import PLA, PROCESS, _cube, _gcode, lib  # noqa: F401


@pytest.fixture()
def app(lib, tmp_path):  # noqa: F811
    a = App(tmp_path / "data", connect=False)
    a.settings.watch_folder = WatchFolder(on=True, path=str(tmp_path / "inbox"))
    a.settings.slice_usual = SliceUsual(machine=MACHINE, process=PROCESS, filament=PLA, plate="Textured PEI Plate",
                                        copies=2)
    (tmp_path / "inbox").mkdir()
    return a


def _drop(app, name: str, data: bytes, age: float = 10) -> None:
    f = app.folder_watch.folder() / name
    f.write_bytes(data)
    t = time.time() - age
    os.utime(f, (t, t))


def test_a_saved_model_is_sliced_with_your_usual_presets(app):
    _drop(app, "bracket.stl", _cube())
    [item] = app.folder_watch.scan()
    assert item["status"] == "ready" and item["name"] == "bracket.gcode.3mf", item["error"]
    data, _ = app.uploads.raw(item["token"])
    g = _gcode(data)
    assert "; curr_bed_type = Textured PEI Plate" in g and "; bed_exclude_area = 0x" in g  # sweep strip kept clear
    assert len(threemf.loads(data).info.object_names) == 2  # your usual two copies
    assert "bracket.stl from your watch folder is sliced" in app.store.events(limit=1)[0]["message"]
    assert app.folder_watch.as_dict()["items"][0]["file"] == "bracket.stl"


def test_each_file_is_taken_once_and_again_when_saved_anew(app):
    _drop(app, "a.stl", _cube())
    assert len(app.folder_watch.scan()) == 1
    assert app.folder_watch.scan() == []  # already taken
    _drop(app, "b.stl", _cube(), age=0)  # still being written
    assert app.folder_watch.scan() == []
    os.utime(app.folder_watch.folder() / "b.stl", (time.time() - 10, time.time() - 10))
    assert [i["file"] for i in app.folder_watch.scan()] == ["b.stl"]
    _drop(app, "a.stl", _cube() + b"\n", age=5)  # saved again with new contents
    assert [i["file"] for i in app.folder_watch.scan()] == ["a.stl"]
    # a restart remembers what was taken
    again = App(app.data_dir, connect=False)
    again.settings.watch_folder = app.settings.watch_folder
    assert again.folder_watch.scan() == [] and len(again.folder_watch.items) == 3


def test_plates_are_taken_as_they_are_and_other_files_left_alone(app):
    sliced = slicing.slice_upload(_cube(), "c.stl", slicing.Choice(machine=MACHINE, process=PROCESS, filament=PLA),
                                  app.data_dir / "w", found=slicing.slicer_and_presets()[0],
                                  lib=slicing.slicer_and_presets()[1])[0]
    _drop(app, "ready.gcode.3mf", sliced)
    _drop(app, "notes.txt", b"hello")
    _drop(app, "broken.3mf", b"not a zip")
    items = {i["file"]: i for i in app.folder_watch.scan()}
    assert set(items) == {"ready.gcode.3mf", "broken.3mf"}
    assert items["ready.gcode.3mf"]["status"] == "ready" and items["ready.gcode.3mf"]["notes"] == ["already sliced"]
    assert items["broken.3mf"]["status"] == "failed" and "can slice" in items["broken.3mf"]["error"]
    assert (app.folder_watch.folder() / "notes.txt").exists()  # nothing in the folder is touched


def test_nothing_happens_while_it_is_off(app):
    app.settings.watch_folder.on = False
    _drop(app, "a.stl", _cube())
    assert app.folder_watch.scan() == []


def test_the_usual_presets_are_made_to_fit_the_printer(lib):  # noqa: F811
    _, presets = lib
    opts = slicing.options(presets)
    hf = {**opts["machines"][0], "flows": ["Standard", "High Flow"]}
    opts = {**opts, "machines": [hf]}
    printer = {"machine": MACHINE, "nozzle_flow": "High Flow", "trays": [{"preset": "Bambu PETG HF @BBL P2S"}]}
    c = slicing.usual_choice({"machine": MACHINE, "process": PROCESS, "filament": "Gone PLA", "copies": 3}, printer, opts)
    assert (c.machine, c.process, c.nozzle_flow, c.copies) == (MACHINE, PROCESS, "High Flow", 3)
    assert c.filament == "Bambu PETG HF @BBL P2S"  # yours doesn't fit any more: what's in the AMS
    assert slicing.usual_choice({}, None, opts).machine == MACHINE  # nothing sliced yet: the 0.4 nozzle preset


def test_the_window_sets_it_and_a_slice_becomes_the_usual(app):
    from fastapi.testclient import TestClient

    from proofloop.app.loop_server import create_app

    c = TestClient(create_app(app), base_url="http://127.0.0.1:8600")
    window = {"X-ProofLoop": "1", "Origin": "http://127.0.0.1:8600"}
    r = c.put("/api/watch-folder", json={"on": False}, headers=window)
    assert r.status_code == 200 and r.json()["on"] is False and r.json()["exists"]
    assert c.put("/api/watch-folder", json={"printer": "nope"}, headers=window).status_code == 422
    model = c.post("/api/plates", files={"file": ("cube.stl", _cube())}, headers=window).json()["model"]
    r = c.post(f"/api/models/{model['token']}/slice", headers=window,
               json={"machine": MACHINE, "process": "0.12mm High Quality @BBL P2S", "filament": PLA, "fill": True})
    assert r.status_code == 200, r.text
    usual = c.get("/api/watch-folder", headers=window).json()["usual"]
    assert usual["process"] == "0.12mm High Quality @BBL P2S" and usual["fill"] is True
