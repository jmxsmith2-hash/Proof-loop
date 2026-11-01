"""LARP mode: a second ProofLoop with pretend printers, its own folder, and twins of your printers."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from proofloop.app import larp
from proofloop.app.shell import HEADER, Shell
from proofloop.core.settings import PrinterConfig, SettingsStore
from proofloop.monitor.printer.state import PrinterState

LARP_ORIGIN = "http://127.0.0.1:8620"
REAL_ORIGIN = "http://127.0.0.1:8600"


def real_printer(nozzle_type="HH01", trays=None):
    state = PrinterState()
    state.merge({"print": {"nozzle_type": nozzle_type, "nozzle_diameter": "0.4", "ams": {"ams": [{"id": "0", "tray": trays or [
        {"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 70, "tray_info_idx": "GFA00",
         "tray_sub_brands": "PLA Basic"},
        {"id": "2", "tray_type": "PETG", "tray_color": "1F1F1FFF", "remain": 40},
        {"id": "3", "tray_type": "ASA-CF", "tray_color": "000000FF", "tray_info_idx": "GFB51"}]}]}}})
    cfg = PrinterConfig(name="Xanders ps2", host="192.168.1.20", serial="01P00A000000001", access_code="secret12",
                        model_id="N7")
    return SimpleNamespace(settings=SimpleNamespace(printers=[cfg]),
                           watches={cfg.name: SimpleNamespace(link=SimpleNamespace(state=state))})


def test_a_twin_copies_model_nozzle_and_ams_and_follows_the_real_printer():
    app = real_printer()
    spec = larp.sync_twins({"speed": 40, "printers": [], "gone_twins": []}, app)
    (t,) = spec["printers"]
    assert (t["name"], t["twin_of"], t["model_id"], t["nozzle"], t["flow"]) == ("Xanders ps2", "Xanders ps2", "N7", 0.4,
                                                                                   "High Flow")
    assert [(x["tray"], x["type"], x["color"], x["remain_pct"]) for x in t["trays"]] == [
        (0, "PLA", "#FF0000", 70), (2, "PETG", "#1F1F1F", 40), (3, "ASA-CF", "#000000", 80)]  # any filament, as it is
    assert "secret12" not in str(spec) and "192.168" not in str(spec)  # nothing to reach the real printer with
    serial = t["serial"]
    app.watches["Xanders ps2"].link.state.merge({"print": {"nozzle_type": "HS01"}})
    (t,) = larp.sync_twins(spec, app)["printers"]
    assert t["flow"] == "Standard" and t["serial"] == serial  # made like it again, still the same pretend printer
    spec = {"speed": 40, "printers": [], "gone_twins": ["Xanders ps2"]}
    assert larp.sync_twins(spec, app)["printers"] == []  # a twin you removed stays removed
    spec = {"speed": 40, "printers": [larp.printer({"printers": []}, "xanders PS2")], "gone_twins": []}
    assert [p["name"] for p in larp.sync_twins(spec, app)["printers"]] == ["xanders PS2", "Xanders ps2 twin"]


def test_pretend_printers_are_checked():
    spec = {"printers": [larp.printer({"printers": []}, "One")]}
    for kw, words in (({"name": "one"}, "already"), ({"name": "a/b"}, "without '/'"),
                      ({"name": "Two", "model_id": "X9"}, "unknown printer model"),
                      ({"name": "Two", "nozzle": 0.5}, "0.2, 0.4"), ({"name": "Two", "flow": "Turbo"}, "Standard or"),
                      ({"name": "Two", "trays": [{"type": "Wood"}]}, "unknown filament"),
                      ({"name": "Two", "trays": [{"type": "PLA", "color": "red"}]}, "colour")):
        with pytest.raises(ValueError, match=words):
            larp.printer(spec, **kw)


def test_larp_keeps_its_own_folder_and_starts_blank_but_ready_to_send(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    monkeypatch.setenv(larp.ENV, "")  # not inside LARP; put back afterwards, as enter() sets it
    assert larp.folder() == tmp_path / "larp" and not larp.inside()
    larp.enter("http://127.0.0.1:8600/")
    assert larp.folder() == tmp_path / "larp" and larp.inside()
    spec = larp.load()
    spec["printers"] = [larp.printer(spec, "A"), {**larp.printer({"printers": []}, "B"), "twin_of": "Real B"}]
    spec["printers"][1]["serial"] = "01P9LARP0000002"
    larp.prepare(spec)
    s = SettingsStore(tmp_path / "larp").load()
    assert s.send_to_printer and [p.name for p in s.printers] == ["A", "B"]
    assert all(p.host == "127.0.0.1" for p in s.printers)
    assert not (tmp_path / "settings.json").exists()  # your real settings: not touched
    s.printers = s.printers[:1]  # B removed under Printers
    SettingsStore(tmp_path / "larp").save(s)
    spec = larp.load()
    larp.prepare(spec)
    assert [p["name"] for p in spec["printers"]] == ["A"] and spec["gone_twins"] == ["Real B"]


@pytest.fixture()
def inside(tmp_path, monkeypatch):
    """The LARP process: its folder, one pretend printer, and its window's server."""
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    monkeypatch.setenv(larp.ENV, "")  # so it is put back afterwards: enter() sets it (delenv records nothing unset)
    larp.enter("http://127.0.0.1:8600/")
    spec = larp.load()
    spec["printers"].append({**larp.printer(spec, "Xanders ps2", flow="High Flow", trays=[{"type": "PLA"}]),
                             "twin_of": "Xanders ps2"})
    farm = larp.Farm(spec)
    monkeypatch.setattr(larp, "FARM", farm)
    larp.prepare(spec)
    for p in spec["printers"]:
        farm.make(p)
    shell = Shell(8620, link_factory=farm.link)
    yield shell, farm, TestClient(shell.api, base_url=LARP_ORIGIN)
    shell.close()
    farm.close()


def online(farm, w, check=lambda s: True):
    next(iter(farm.fakes.values())).wait_for(lambda: w.link.state.online and check(w.link.state), timeout=15)


def test_inside_larp_every_printer_is_pretend(inside):
    shell, farm, c = inside
    win = {HEADER: "1", "Origin": LARP_ORIGIN}
    assert c.get("/proofloop/ping").json()["larp"] is True
    assert c.get("/proofloop/larp").status_code == 403  # the window's own
    s = c.get("/proofloop/larp", headers=win).json()
    assert s["inside"] and s["back"] == "http://127.0.0.1:8600/" and s["speed"] == 40
    assert [p["name"] for p in s["printers"]] == ["Xanders ps2"]
    online(farm, shell.monitor.watches["Xanders ps2"], lambda st: st.nozzle_flow == "High Flow")

    assert c.post("/proofloop/larp/speed", json={"speed": 100}, headers=win).json()["speed"] == 100
    assert all(f.layer_s == larp.LAYER_S / 100 and f.step_s == larp.STEP_S / 100 for f in farm.fakes.values())
    assert c.post("/proofloop/larp/speed", json={"speed": 0}, headers=win).status_code == 422
    assert larp.load()["speed"] == 100  # kept for next time

    r = c.post("/proofloop/larp/add", json={"name": "Spare", "model_id": "C12", "nozzle": 0.6, "flow": "High Flow",
                                            "trays": [{"type": "PETG", "color": "#112233"}, {"type": ""}]}, headers=win)
    assert r.status_code == 200, r.text
    spare = shell.monitor.watches["Spare"]
    online(farm, spare, lambda st: st.nozzle_diameter == 0.6 and st.ams.trays())
    assert spare.link.state.nozzle_flow == "High Flow"
    assert [(t["type"], t["color"]) for t in spare.link.state.ams.trays()] == [("PETG", "#112233")]
    assert c.post("/proofloop/larp/add", json={"name": "spare"}, headers=win).status_code == 422

    # added under Printers like a real one, with a real address: it gets a pretend printer anyway
    w = shell.monitor.add_printer(PrinterConfig(name="Typed", host="192.168.1.50", serial="01P00A0000000T1",
                                                access_code="abcd1234", model_id="N7"))
    assert w.link.host == "127.0.0.1"
    online(farm, w)
    assert "Typed" in [p["name"] for p in larp.load()["printers"]]

    r = c.post("/proofloop/larp/remove", json={"name": "Xanders ps2"}, headers=win).json()
    assert [p["name"] for p in r["printers"]] == ["Spare", "Typed"] and "Xanders ps2" not in shell.monitor.watches
    assert larp.load()["gone_twins"] == ["Xanders ps2"]


def test_the_real_window_turns_larp_on_and_off(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    monkeypatch.setenv(larp.ENV, "")  # the real ProofLoop, not LARP
    calls = []
    monkeypatch.setattr(larp, "running", lambda: False)
    monkeypatch.setattr(larp, "start", lambda app, back: calls.append(("start", back)) or "http://127.0.0.1:8620/")
    monkeypatch.setattr(larp, "stop", lambda: calls.append(("stop",)) or True)
    shell = Shell(8600, connect=False)
    try:
        c = TestClient(shell.api, base_url=REAL_ORIGIN)
        win = {HEADER: "1", "Origin": REAL_ORIGIN}
        assert c.get("/proofloop/ping").json()["larp"] is False
        assert c.get("/proofloop/larp", headers=win).json() == {"inside": False, "running": False,
                                                                 "url": "http://127.0.0.1:8620/"}
        assert c.post("/proofloop/larp", json={"on": True}).status_code == 403
        assert c.post("/proofloop/larp", json={"on": True}, headers=win).json()["url"] == "http://127.0.0.1:8620/"
        assert c.post("/proofloop/larp", json={"on": False}, headers=win).json() == {"running": False}
        assert calls == [("start", "http://127.0.0.1:8600/"), ("stop",)]
        assert c.post("/proofloop/larp/speed", json={"speed": 5}, headers=win).status_code in (404, 405)
    finally:
        shell.close()


def test_the_background_service_ends_larp_when_it_stops(tmp_path, monkeypatch):
    from tests.app.test_service import make

    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    monkeypatch.setenv(larp.ENV, "")
    stopped = []
    monkeypatch.setattr(larp, "stop", lambda: stopped.append(1) or True)
    svc = make(tmp_path)
    try:
        assert larp.stop in svc.on_stop  # registered as the window's server is built, before the rest
    finally:
        svc.stop("test")
    assert stopped == [1]
