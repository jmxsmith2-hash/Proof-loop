"""Sending a loop file to a printer: off unless turned on, and only when everything checks out."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import PrinterConfig
from proofloop.monitor.printer.state import PrinterState
from proofloop.monitor.send import SendError, match_trays, plan, start_command, upload
from proofloop.monitor.service import App

PLATE = Path(__file__).resolve().parents[2] / "examples" / "gcode" / "bracket_plate.gcode.3mf"
WHITE_PLA = {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "F0F0F0FF"},
                                                  {"id": "1", "tray_type": "PETG", "tray_color": "000000FF"}]}]}}


def state(gcode_state="IDLE", online=True, ams=WHITE_PLA):
    s = PrinterState(online=online, gcode_state=gcode_state)
    if ams:
        s.merge({"print": ams})
    return s


def test_each_slot_gets_the_nearest_tray_of_its_type():
    trays = state().ams.trays()
    assert match_trays([("1", "PLA", "#F2F2F2")], trays) == ([0], [])
    assert match_trays([("2", "PETG", "#000000")], trays) == ([-1, 1], [])
    mapping, problems = match_trays([("1", "PLA", "#FF0000")], trays)  # loaded, but white
    assert problems == ["no PLA #FF0000 loaded for slot 1"]
    assert match_trays([("1", "ABS", "#F2F2F2")], trays)[1]


@pytest.mark.parametrize("st, model, danger, why", [
    (state(online=False), "N7", [], "isn't connected"),
    (state("RUNNING"), "N7", [], "is busy (running)"),
    (state("PAUSE"), "N7", [], "is busy (pause)"),
    (state(), "O1D", [], "sliced for another printer model"),
    (state(), "N7", ["Parts the sweep passes over"], "the loop-file checks found: Parts the sweep passes over"),
])
def test_refuses_anything_unsafe(st, model, danger, why):
    with pytest.raises(SendError, match=why.replace("(", r"\(").replace(")", r"\)")):
        plan("p2s", st, model, "N7", [("1", "PLA", "#F2F2F2")], danger)


def test_without_an_ams_only_a_one_filament_file_goes_to_the_external_spool():
    p = plan("p2s", state(ams=None), "N7", "N7", [("1", "PLA", "#F2F2F2")], [])
    assert not p.use_ams and p.ams_mapping == []
    with pytest.raises(SendError, match="has no AMS"):
        plan("p2s", state(ams=None), "N7", "N7", [("1", "PLA", "#F2F2F2"), ("2", "PLA", "#000000")], [])


def test_the_start_command_names_the_file_plate_and_trays():
    p = plan("p2s", state("FINISH"), "N7", "N7", [("1", "PLA", "#F2F2F2")], [], plate=2)
    p.filename = "FL_S2_bracket.gcode.3mf"
    cmd = start_command(p)["print"]
    assert cmd["command"] == "project_file" and cmd["param"] == "Metadata/plate_2.gcode"
    assert cmd["url"] == "file:///sdcard/FL_S2_bracket.gcode.3mf" and cmd["subtask_name"] == "FL_S2_bracket"
    assert cmd["use_ams"] is True and cmd["ams_mapping"] == [0]


def test_a_printer_that_cant_be_reached_says_so():
    with pytest.raises(SendError, match="couldn't copy the file"):
        upload("127.0.0.1", "12345678", "x.gcode.3mf", b"x", timeout=2)  # nothing listens on 990 here


class FakeLink:
    def __init__(self, p):
        self.state = state("IDLE")
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


def test_send_from_make_a_loop_file(farm):
    c = TestClient(create_app(farm))
    token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
    body = {"printer": "p2s", "confirm": "p2s", "loops": 2}
    r = c.post(f"/api/plates/{token}/send", json=body)
    assert r.status_code == 409 and "sending to printers is off" in r.json()["detail"]
    assert c.get("/api/state").json()["send_to_printer"] is False
    assert c.put("/api/send-to-printer", json={"on": True}).json() == {"send_to_printer": True}
    assert c.post(f"/api/plates/{token}/send", json={**body, "confirm": "other"}).status_code == 400
    farm.watches["p2s"].link.state.gcode_state = "RUNNING"
    r = c.post(f"/api/plates/{token}/send", json=body)
    assert r.status_code == 409 and "busy" in r.json()["detail"]
    farm.watches["p2s"].link.state.gcode_state = "FINISH"
    r = c.post(f"/api/plates/{token}/send", json=body)
    assert r.status_code == 200, r.text
    name, head, cmd = farm.watches["p2s"].link.sent[0]
    assert name.startswith("FL_S2_") and head == b"PK" and cmd["print"]["ams_mapping"] == [0]
    assert r.json()["file"] == name
    assert any(e["kind"] == "sent" for e in farm.store.events())
    assert (farm.data_dir / "files" / name).is_file()  # kept, so the journal can check what ran


def test_a_paired_phone_can_never_send(farm):
    farm.settings.send_to_printer = True
    phone = TestClient(create_app(farm, allow_send=False))
    token = phone.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
    assert phone.post(f"/api/plates/{token}/send", json={"printer": "p2s", "confirm": "p2s"}).status_code == 403
    assert phone.put("/api/send-to-printer", json={"on": False}).status_code == 403
    assert phone.get("/api/state").json()["send_to_printer"] is False and not farm.watches["p2s"].link.sent
