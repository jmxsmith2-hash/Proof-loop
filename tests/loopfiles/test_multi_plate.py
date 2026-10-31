"""Files with several sliced plates ('Export all sliced file'): every plate can be used, not just the first."""

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import PrinterConfig
from proofloop.loopfiles import threemf
from proofloop.monitor.service import App
from tests.monitor.test_send import FakeLink as SendLink
from tests.plates import BUCKET, SMALL, plain, two_plates

PLATE = Path(__file__).resolve().parents[2] / "examples" / "gcode" / "bracket_plate.gcode.3mf"


def test_each_plate_comes_out_on_its_own_and_keeps_its_number():
    both = two_plates(plain(BUCKET), plain(SMALL))
    assert threemf.sliced_plates(both) == [1, 2]
    assert threemf.sliced_plates(plain(SMALL)) == [1] and threemf.sliced_plates(b"not a zip") == []
    two = threemf.only_plate(both, 2)
    names = zipfile.ZipFile(io.BytesIO(two)).namelist()
    assert "Metadata/plate_2.gcode" in names and "Metadata/top_2.png" in names
    assert not any(n.startswith(("Metadata/plate_1", "Metadata/top_1")) for n in names)
    assert "Metadata/slice_info.config" in names and "3D/3dmodel.model" in names
    sf = threemf.loads(two)
    assert sf.plate == 2 and sf.md5_ok and set(sf.info.object_names) == {"1", "2"}  # plate 2's own parts
    assert threemf.loads(threemf.only_plate(both, 1)).info.object_names.keys() == {"93", "104"}
    with pytest.raises(threemf.PlateFileError):
        threemf.only_plate(both, 3)
    assert threemf.plate_name("widget.gcode.3mf", 2) == "widget-plate2.gcode.3mf"
    assert threemf.plate_name("Widget.3MF", 1) == "Widget-plate1.3MF"


def test_dropping_a_multi_plate_file_offers_every_plate(tmp_path):
    app = App(tmp_path, connect=False)
    c = TestClient(create_app(app))
    r = c.post("/api/plates", files={"file": ("pair.gcode.3mf", two_plates(plain(BUCKET), plain(SMALL)))}).json()
    assert [(p["plate"], p["file"]) for p in r["plates"]] == [(1, "pair-plate1.gcode.3mf"), (2, "pair-plate2.gcode.3mf")]
    assert r["token"] == r["plates"][0]["token"] and r["file"] == "pair-plate1.gcode.3mf" and r["ok"]
    second = c.post(f"/api/plates/{r['plates'][1]['token']}/open").json()
    assert second["file"] == "pair-plate2.gcode.3mf" and len(second["parts"]) == 2 and second["ok"]
    d = c.post(f"/api/plates/{second['token']}/download", json={"loops": 2})
    assert d.status_code == 200 and d.headers["x-proofloop-filename"] == "FL_S2_pair-plate2.gcode.3mf"
    made = threemf.loads(d.content)
    assert made.plate == 2 and made.gcode.count("; EXECUTABLE_BLOCK_START") == 2
    batch = {"items": [{"token": p["token"], "count": 1} for p in r["plates"]], "settings": {}}
    assert c.post("/api/batch/preview", json=batch).json()["loops"] == 2  # both plates, one file
    one = c.post("/api/plates", files={"file": ("one.gcode.3mf", plain(SMALL))}).json()
    assert "plates" not in one and one["file"] == "one.gcode.3mf"  # a single plate is as before


def test_sending_the_second_plate_starts_the_second_plate(tmp_path):
    app = App(tmp_path, connect=False, link_factory=SendLink)
    app.add_printer(PrinterConfig(name="p2s", host="127.0.0.1", serial="01", access_code="12345678", model_id="N7"))
    app.settings.send_to_printer = True
    try:
        c = TestClient(create_app(app))
        data = two_plates(PLATE.read_bytes(), PLATE.read_bytes())
        plates = c.post("/api/plates", files={"file": ("pair.gcode.3mf", data)}).json()["plates"]
        app.watches["p2s"].link.state.gcode_state = "FINISH"
        r = c.post(f"/api/plates/{plates[1]['token']}/send", json={"printer": "p2s", "confirm": "p2s", "loops": 1})
        assert r.status_code == 200, r.text
        name, _, cmd = app.watches["p2s"].link.sent[0]
        assert name == "FL_S2_pair-plate2.gcode.3mf" and cmd["print"]["param"] == "Metadata/plate_2.gcode"
    finally:
        app.shutdown()
