"""Print again: a loop file ProofLoop made and kept is listed, and sent again through the normal send path."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import PrinterConfig
from proofloop.monitor.printer.state import PrinterState
from proofloop.monitor.service import App

PLATE = Path(__file__).resolve().parents[2] / "examples" / "gcode" / "bracket_plate.gcode.3mf"
WHITE_PLA = {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "F0F0F0FF"}]}]}}


class FakeLink:
    def __init__(self, p):
        self.state = PrinterState(online=True, gcode_state="FINISH")
        self.state.merge({"print": WHITE_PLA})
        self.sent = []

    def add_listener(self, fn):
        pass

    def connect(self):
        pass

    def send(self, filename, data, command):
        self.sent.append((filename, data[:2], command))


@pytest.fixture()
def farm(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    app.add_printer(PrinterConfig(name="p2s", host="127.0.0.1", serial="01", access_code="12345678", model_id="N7"))
    yield app
    app.shutdown()


def make(c, loops=2):
    """Make a loop file the way Download does: it is recorded and kept. Returns the history row."""
    token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
    assert c.post(f"/api/plates/{token}/download", json={"loops": loops}).status_code == 200
    rows = c.get("/api/made").json()
    assert len(rows) == 1
    return rows[0]


def test_the_history_lists_made_files_with_when_printer_and_loops(farm):
    c = TestClient(create_app(farm))
    assert c.get("/api/made").json() == []
    row = make(c, loops=2)
    assert row["file"].endswith(".gcode.3mf") and row["key"] and row["made"] > 0
    assert row["loops"] == 2 and row["printer"] is None and row["sent"] is None and row["kept"] is True


def test_print_again_sends_the_kept_file_through_the_send_path(farm):
    c = TestClient(create_app(farm))
    key = make(c)["key"]
    farm.settings.send_to_printer = True

    targets = c.post(f"/api/made/{key}/printers", json={}).json()
    assert targets["send_to_printer"] is True
    p2s = next(t for t in targets["printers"] if t["printer"] == "p2s")
    assert p2s["ready"] and p2s["slots"][0]["tray"] == 0  # the same AMS tray matching as a plate's send

    body = {"printer": "p2s", "confirm": "p2s"}
    assert c.post(f"/api/made/{key}/send", json={**body, "confirm": "other"}).status_code == 400
    farm.watches["p2s"].link.state.gcode_state = "RUNNING"
    r = c.post(f"/api/made/{key}/send", json=body)
    assert r.status_code == 409 and "busy" in r.json()["detail"]
    farm.watches["p2s"].link.state.gcode_state = "FINISH"

    r = c.post(f"/api/made/{key}/send", json=body)
    assert r.status_code == 200, r.text
    path, head, cmd = farm.watches["p2s"].link.sent[0]
    assert path.startswith("cache/FL_") and path.endswith(".gcode.3mf") and head == b"PK"
    assert cmd["print"]["command"] == "project_file" and cmd["print"]["ams_mapping"] == [0]
    assert any(e["kind"] == "sent" for e in farm.store.events())

    row = c.get("/api/made").json()[0]
    assert row["printer"] == "p2s" and row["sent"] is not None and row["kept"] is True


def test_print_again_needs_sending_turned_on(farm):
    c = TestClient(create_app(farm))
    key = make(c)["key"]
    r = c.post(f"/api/made/{key}/send", json={"printer": "p2s", "confirm": "p2s"})
    assert r.status_code == 409 and "sending to printers is off" in r.json()["detail"]
    assert not farm.watches["p2s"].link.sent


def test_a_file_no_longer_kept_is_listed_as_such_and_cant_be_sent(farm):
    c = TestClient(create_app(farm))
    key = make(c)["key"]
    farm.settings.send_to_printer = True
    for kept in (farm.data_dir / "files").glob("*.gcode.3mf"):
        kept.unlink()  # the kept copy is gone (the folder keeps only the newest files)

    assert c.get("/api/made").json()[0]["kept"] is False
    r = c.post(f"/api/made/{key}/send", json={"printer": "p2s", "confirm": "p2s"})
    assert r.status_code == 410 and "no longer kept" in r.json()["detail"]
    assert c.post(f"/api/made/{key}/printers", json={}).status_code == 410
    assert not farm.watches["p2s"].link.sent


def test_an_unknown_file_is_not_found(farm):
    c = TestClient(create_app(farm))
    farm.settings.send_to_printer = True
    assert c.post("/api/made/no_such_file/send", json={"printer": "p2s", "confirm": "p2s"}).status_code == 404
    assert c.post("/api/made/no_such_file/printers", json={}).status_code == 404


def test_a_paired_phone_can_list_but_never_print_again(farm):
    key = make(TestClient(create_app(farm)))["key"]
    farm.settings.send_to_printer = True
    phone = TestClient(create_app(farm, allow_send=False))
    assert phone.get("/api/made").status_code == 200
    assert phone.post(f"/api/made/{key}/printers", json={}).status_code == 403
    assert phone.post(f"/api/made/{key}/send", json={"printer": "p2s", "confirm": "p2s"}).status_code == 403
    assert not farm.watches["p2s"].link.sent
