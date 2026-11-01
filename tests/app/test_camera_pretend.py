"""A pretend printer printing a loop file with the timelapse on: the real threads, sockets and camera."""

import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

pytest.importorskip("cryptography")

from proofloop.app import pretend_farm  # noqa: E402
from proofloop.app.loop_server import create_app  # noqa: E402
from proofloop.monitor.printer.pretend import FakePrinter  # noqa: E402
from proofloop.monitor.service import App  # noqa: E402
from tests.monitor.test_pretend_printer import PLATE  # noqa: E402


def test_a_pretend_print_leaves_a_timelapse_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFLOOP_PRETEND", "1")
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    farm = [FakePrinter(serial="22E8BJ500000000", simulate=True, layer_s=0.05, step_s=0.001, person_s=5)]
    farm[0].state["ams"] = {"ams": pretend_farm._trays(0), "tray_now": "255"}
    try:
        assert pretend_farm.prepare(farm) is True
        pretend_farm.add_spools(tmp_path)
        app = App(tmp_path, connect=True, link_factory=pretend_farm.link_factory(farm))
        w = next(iter(app.watches.values()))
        farm[0].wait_for(lambda: w.link.state.online and w.link.state.ams.trays())
        c = TestClient(create_app(app))
        assert c.put("/api/camera-settings", json={"timelapse": True, "timelapse_every_layers": 4}).status_code == 200
        token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
        r = c.post(f"/api/plates/{token}/send", json={"printer": w.name, "confirm": w.name, "loops": 1})
        assert r.status_code == 200, r.text
        farm[0].wait_for(lambda: w.link.state.gcode_state == "FINISH", timeout=60)
        deadline = time.time() + 20
        while time.time() < deadline and not c.get(f"/api/monitor/{w.name}/timelapses").json():
            time.sleep(0.2)  # the animation is made in the background when the print ends
        (item,) = c.get(f"/api/monitor/{w.name}/timelapses").json()
        assert item["frames"] >= 3 and item["outcome"] == "finished"
        shown = c.get(f"/api/monitor/{w.name}/timelapses/{item['id']}")
        assert shown.status_code == 200 and len(shown.content) == item["size"]
        assert not [p for p in w.timelapse.dir.iterdir() if p.is_dir()]  # the pictures are gone
        assert Image.open(w.timelapse.dir / f"{item['id']}.{item['kind']}").width <= 640
        app.shutdown()
    finally:
        for p in farm:
            p.close()
