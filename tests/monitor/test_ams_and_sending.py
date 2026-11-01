"""A printer whose AMS shows up, spools made from it, and seeing before you send whether a printer is ready."""

import ast
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import PrinterConfig
from proofloop.monitor.service import App
from tests.monitor.test_send import PLATE, FakeLink, state

SRC = Path(__file__).resolve().parents[2] / "src" / "proofloop"
RFID = {"ams": {"ams": [{"id": "0", "humidity_raw": "31", "tray": [
    {"id": "0", "tray_type": "PLA", "tray_color": "F0F0F0FF", "tray_sub_brands": "PLA Basic", "tray_weight": "1000",
     "remain": 40, "tray_uuid": "A1B2C3D4E5F60718293A4B5C6D7E8F90", "tray_info_idx": "GFA00"},
    {"id": "1", "tray_type": "ASA-CF", "tray_color": "000000FF", "remain": -1},
    {"id": "2", "tray_type": ""}]}]}}


@pytest.fixture()
def farm(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    app.add_printer(PrinterConfig(name="p2s", host=" 192.168.1.90 ", serial="22e8aj 590900543", access_code="12345678",
                                  model_id="N7"))
    yield app
    app.shutdown()


def test_a_serial_typed_in_lower_case_still_reaches_the_printer(farm):
    cfg = farm.printer_config("p2s")
    assert cfg.serial == "22E8AJ590900543" and cfg.host == "192.168.1.90"  # MQTT topics are case-sensitive


def test_connected_but_silent_says_to_check_the_serial(farm):
    w, s = farm.watches["p2s"], farm.watches["p2s"].link.state
    s.online, s.reports, s.connected_at = True, 0, time.time() - 60
    assert "check its serial number" in w.as_dict()["status"]
    s.merge({"print": {"gcode_state": "IDLE"}})
    assert s.reports == 1 and "serial" not in w.as_dict()["status"]


def test_every_tray_becomes_a_linked_spool_in_one_go(farm):
    farm.watches["p2s"].link.state.merge({"print": RFID})
    c = TestClient(create_app(farm))
    r = c.post("/api/filament/ams-spools", json={"printer": "p2s"}).json()
    assert r["added"] == 2
    by_tray = {s["tray"]: s for s in c.get("/api/filament").json()["spools"]}
    pla, asa = by_tray[0], by_tray[1]
    assert (pla["printer"], pla["material"], pla["name"], pla["brand"]) == ("p2s", "PLA", "PLA Basic", "Bambu Lab")
    assert pla["net_g"] == 1000 and pla["remaining_g"] == 400 and pla["filament_id"] == "GFA00" and pla["tag"]
    assert asa["material"] == "ASA-CF" and asa["remaining_g"] is None and asa["net_g"] is None  # no tag: not guessed
    assert c.post("/api/filament/ams-spools", json={}).json()["added"] == 0  # already linked
    assert c.post("/api/filament/ams-spools", json={"printer": "nope"}).status_code == 404


def test_you_see_which_printer_is_ready_and_which_tray_feeds_each_slot(farm):
    c = TestClient(create_app(farm))
    token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
    link = farm.watches["p2s"].link
    link.state, link.state.reports = state("FINISH"), 1
    r = c.post(f"/api/plates/{token}/printers").json()
    assert r["send_to_printer"] is False
    (t,) = r["printers"]
    assert t["ready"] and t["reasons"] == [] and t["slots"] == [
        {"slot": "1", "type": "PLA", "color": "#F2F2F2", "tray": 0, "tray_label": "A1"}]
    link.state.gcode_state = "RUNNING"
    (t,) = c.post(f"/api/plates/{token}/printers").json()["printers"]
    assert not t["ready"] and t["reasons"] == ["p2s is busy (running)"]
    link.state = state("IDLE", ams={"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PETG",
                                                                          "tray_color": "000000FF"}]}]}})
    (t,) = c.post(f"/api/plates/{token}/printers").json()["printers"]
    assert not t["ready"] and t["slots"][0]["tray"] is None and "no PLA #F2F2F2 loaded for slot 1" in t["reasons"]


def test_readiness_is_never_offered_where_sending_is_not_allowed(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    try:
        c = TestClient(create_app(app, allow_send=False))
        token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
        assert c.post(f"/api/plates/{token}/printers").status_code == 403
    finally:
        app.shutdown()


def test_no_program_proofloop_starts_opens_a_console_window():
    """ProofLoop runs without a console: on Windows each program it starts would pop up a window of its own."""
    missing = []
    for f in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("run", "Popen") and getattr(node.func.value, "id", "") == "subprocess"
                    and not any(k.arg == "creationflags" for k in node.keywords) and not any(
                        isinstance(k.value, ast.Name) and k.arg is None for k in node.keywords)):
                missing.append(f"{f.relative_to(SRC)}:{node.lineno}")
    assert not missing, f"start these with creationflags=NO_WINDOW: {missing}"


def test_a_plate_sliced_for_another_nozzle_flow_is_flagged_before_sending(farm):
    from proofloop.loopfiles import threemf
    from proofloop.monitor.send import check
    from tests.plates import SMALL, make_3mf, plate_gcode

    gcode = plate_gcode(SMALL).replace("; printable_height", "; nozzle_volume_type = Standard\n; printable_height", 1)
    sf = threemf.loads(make_3mf(gcode, SMALL))
    assert sf.info.nozzle_flow == "Standard"
    s = state("IDLE")
    s.merge({"print": {"nozzle_type": "HH01", "nozzle_diameter": "0.4"}})  # what a P2S with a high-flow nozzle says
    assert (s.nozzle_flow, s.nozzle_diameter, s.as_dict()["nozzle"]["flow"]) == ("High Flow", 0.4, "High Flow")
    slots = [("1", "PLA", "#F2F2F2")]
    p, reasons = check("p2s", s, "N7", "N7", slots, [], nozzle="0.4", nozzle_flow="Standard")
    assert not reasons and p.notes == ["sliced for a Standard nozzle, but p2s has a High Flow one: slice it again in "
                                       "Bambu Studio with the nozzle flow set to High Flow"]
    assert check("p2s", s, "N7", "N7", slots, [], nozzle="0.4", nozzle_flow="High Flow")[0].notes == []
    _, reasons = check("p2s", s, "N7", "N7", slots, [], nozzle="0.6", nozzle_flow="High Flow")
    assert reasons == ["the file was sliced for a 0.6 mm nozzle, p2s has a 0.4 mm one"]
    s.nozzle_type = "hardened_steel"  # older firmware: no flow in the code, so nothing to compare
    assert check("p2s", s, "N7", "N7", slots, [], nozzle="0.4", nozzle_flow="Standard")[0].notes == []


def test_a_p2s_gets_its_file_in_cache_and_the_newer_start_command():
    """file:///sdcard/<file> fails on a P2S with 0500-4002 (unsupported file path or name)."""
    from proofloop.monitor.send import Plan, plate_facts, printer_filename, remote_path, start_command
    from tests.plates import SMALL, make_3mf, plate_gcode

    assert printer_filename("FL_S1_bootv2.3mf") == "FL_S1_bootv2.gcode.3mf"
    assert printer_filename("FL_S1_boot_crochet_1 Boot (red) - print once.gcode.3mf") == \
        "FL_S1_boot_crochet_1_Boot_red_-_print_once.gcode.3mf"
    assert remote_path("N7", "a.gcode.3mf") == "cache/a.gcode.3mf" and remote_path("C12", "a.gcode.3mf") == "a.gcode.3mf"
    data = make_3mf(plate_gcode(SMALL), SMALL)
    md5, filaments = plate_facts(data, 1)
    assert len(md5) == 32 and plate_facts(b"not a zip", 1) == ("", 0)
    p = Plan("p2s", "a.gcode.3mf", 1, True, [-1, 5])
    cmd = start_command(p, "N7", md5, max(filaments, 3))["print"]
    assert cmd["url"] == "ftp:///cache/a.gcode.3mf" and cmd["file"] == "a.gcode.3mf" and cmd["md5"] == md5
    assert cmd["ams_mapping"] == [-1, 5, -1]
    assert cmd["ams_mapping2"] == [{"ams_id": 255, "slot_id": 255}, {"ams_id": 1, "slot_id": 1},
                                   {"ams_id": 255, "slot_id": 255}]
    assert cmd["task_id"] != "0" and cmd["bed_leveling"] == 1
    old = start_command(p, "C12")["print"]  # other models keep the form they take
    assert old["url"] == "file:///sdcard/a.gcode.3mf" and "ams_mapping2" not in old
