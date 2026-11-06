"""Auto-eject for a single plate: "Clear the bed when done" ends one print with the cooldown and sweep."""

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import PrinterConfig
from proofloop.loopfiles import threemf
from proofloop.monitor.service import App
from tests.monitor.test_send import PLATE, FakeLink


@pytest.fixture()
def farm(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    app.add_printer(PrinterConfig(name="p2s", host="127.0.0.1", serial="01", access_code="12345678", model_id="N7"))
    app.settings.send_to_printer = True
    app.watches["p2s"].link.state.gcode_state = "FINISH"
    yield app
    app.shutdown()


def upload(c):
    return c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]


def one_plate_send(farm, **extra):
    c = TestClient(create_app(farm))
    token = upload(c)
    r = c.post(f"/api/plates/{token}/send", json={"printer": "p2s", "confirm": "p2s", "loops": 1, **extra})
    assert r.status_code == 200, r.text
    return c, token, farm.watches["p2s"].link.sent[-1]


def test_auto_eject_is_on_by_default_and_a_single_print_gets_the_sweep(farm):
    assert farm.settings.defaults.auto_eject is True
    c, token, (path, _, _) = one_plate_send(farm)
    assert path.removeprefix("cache/").startswith("FL_S2_")
    made = c.post(f"/api/plates/{token}/download", json={"loops": 1, "auto_eject": True})
    gcode = threemf.loads(made.content).gcode
    assert gcode.count("; EXECUTABLE_BLOCK_START") == 1 and "PROOFLOOP END" in gcode and "M190 S30" in gcode


def test_auto_eject_off_sends_the_plate_as_sliced(farm):
    c, token, (path, _, _) = one_plate_send(farm, auto_eject=False)
    assert not path.removeprefix("cache/").startswith("FL_S")
    made = c.post(f"/api/plates/{token}/download", json={"loops": 1, "auto_eject": False})
    gcode = threemf.loads(made.content).gcode
    assert "PROOFLOOP" not in gcode and made.headers["x-proofloop-filename"] == PLATE.name
    # more than one loop always clears the bed: the tick box only applies to a single print
    many = c.post(f"/api/plates/{token}/download", json={"loops": 2, "auto_eject": False})
    assert threemf.loads(many.content).gcode.count(">>> PROOFLOOP END") == 2


def test_auto_eject_still_asks_to_confirm_the_printer(farm):
    c = TestClient(create_app(farm))
    token = upload(c)
    r = c.post(f"/api/plates/{token}/send", json={"printer": "p2s", "loops": 1, "auto_eject": True})
    assert r.status_code == 400 and not farm.watches["p2s"].link.sent


def test_a_paired_phone_cannot_send_with_auto_eject(farm):
    phone = TestClient(create_app(farm, allow_send=False))
    token = upload(phone)
    r = phone.post(f"/api/plates/{token}/send", json={"printer": "p2s", "confirm": "p2s", "auto_eject": True})
    assert r.status_code == 403 and not farm.watches["p2s"].link.sent


def test_the_page_has_the_tick_box_ticked_and_wired_up():
    from pathlib import Path

    static = Path(__file__).resolve().parents[2] / "src" / "proofloop" / "app" / "static"
    assert 'id="sEject" checked' in (static / "loop.html").read_text(encoding="utf-8")
    js = (static / "js" / "make.js").read_text(encoding="utf-8")
    assert "auto_eject: $('#sEject').checked" in js
