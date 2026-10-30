"""``proofloop try``: pretend printers that print by themselves, walking each file's own G-code."""

import json

from fastapi.testclient import TestClient

from proofloop.app import pretend_farm
from proofloop.app.loop_server import create_app
from proofloop.core.settings import SettingsStore
from proofloop.loopfiles import threemf
from proofloop.monitor.printer.pretend import FakePrinter, steps
from proofloop.monitor.service import App
from tests.monitor.test_pretend_printer import PLATE


def test_the_steps_come_from_the_gcode():
    gcode = "; total layer number: 2\nM104 S1\n; CHANGE_LAYER\nG1 X1\n; CHANGE_LAYER\nM104 S2 ; door\nM400 U1\n" \
            "; total layer number: 2\n; CHANGE_LAYER\n; CHANGE_LAYER\nM104 S5\n"
    assert steps(gcode) == [("layers", 2), ("signal", 1), ("layer", 1), ("layer", 2), ("signal", 2), ("pause", 0),
                            ("layers", 2), ("layer", 1), ("layer", 2), ("signal", 5)]


def test_a_pretend_printer_prints_a_loop_file_with_proofloop_watching(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFLOOP_PRETEND", "1")  # ProofLoop's folder is the farm's own
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    farm = [FakePrinter(serial="22E8BJ500000000", simulate=True, layer_s=0.001, step_s=0.001, person_s=5)]
    farm[0].state["ams"] = {"ams": pretend_farm._trays(0), "tray_now": "255"}
    try:
        assert pretend_farm.prepare(farm) is True
        s = SettingsStore(tmp_path).load()
        assert s.send_to_printer and s.defaults.wait_for_monitor and len(s.printers) == 1
        pretend_farm.add_spools(tmp_path)
        app = App(tmp_path, connect=True, link_factory=pretend_farm.link_factory(farm))
        w = next(iter(app.watches.values()))
        farm[0].wait_for(lambda: w.link.state.online and w.link.state.ams.trays())
        c = TestClient(create_app(app))
        token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
        r = c.post(f"/api/plates/{token}/send", json={"printer": w.name, "confirm": w.name, "loops": 3,
                                                       "wait_for_monitor": True})
        assert r.status_code == 200, r.text
        farm[0].wait_for(lambda: w.link.state.gcode_state == "FINISH", timeout=60)
        resumes = [x for x in farm[0].commands if x.get("command") == "resume"]
        assert len(resumes) == 2  # the plate checked and the next loop resumed, twice: three loops
        made = app.store.production()
        assert len(made) == 3 and all(p["ok"] for p in made)
        spool = next(sp for sp in app.inventory.all() if sp.printer == w.name)  # linked by its RFID tag
        assert spool.remaining_g < 820
        app.shutdown()
    finally:
        for p in farm:
            p.close()


def test_the_farm_keeps_its_own_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    monkeypatch.delenv("PROOFLOOP_PRETEND", raising=False)
    assert pretend_farm.folder() == tmp_path / "try"
    monkeypatch.setenv("PROOFLOOP_PRETEND", "1")
    assert pretend_farm.folder() == tmp_path
    assert json.loads(json.dumps(threemf.load(PLATE).info.filament_slots))  # the example plate the farm runs
