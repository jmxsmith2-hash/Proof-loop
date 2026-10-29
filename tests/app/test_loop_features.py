"""Batch files, change view, sweep path, discovery, notifications with pictures, quiet hours."""

import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.notify import requests_for
from proofloop.core.settings import QuietHours
from proofloop.loopfiles import threemf
from proofloop.loopfiles.gcode.processor import ProcessError, analyze, changes, process_batch
from proofloop.loopfiles.gcode.routines import LoopSettings
from proofloop.loopfiles.profiles import ProfileStore
from proofloop.monitor.printer.discovery import parse
from proofloop.monitor.service import App
from proofloop.monitor.store import Store
from proofloop.monitor.watch import PrinterWatch
from tests.app.test_loop_server import FakeLink, up
from tests.monitor.test_monitor import Link, Verifier
from tests.plates import BUCKET, SMALL, make_3mf, plain, plate_gcode

P = ProfileStore().get("p2s-stage2-farmboard")


def sf(data, name):
    return threemf.loads(data, name=name)


def test_batch_keeps_order_and_pauses_between_every_plate():
    a, b = sf(plain(BUCKET), "a.gcode.3mf"), sf(plain(SMALL), "b.gcode.3mf")
    r = process_batch([(a, 2), (b, 1)], P, LoopSettings(wait_for_monitor=True))
    g = r.gcode
    assert r.filename == "FL_S2_batch-3x-a.gcode.3mf"
    heights = re.findall(r"^; max_z_height: (\S+)", g, re.M)
    assert heights == ["170.00", "170.00", "12.00"]
    assert len(re.findall(r"^M400 U1$", g, re.M)) == 2
    assert "loop 3/3" in g and threemf.loads(r.data).md5_ok


def test_batch_refuses_plates_that_cant_share_a_file():
    a = sf(plain(BUCKET), "a")
    petg = sf(make_3mf(plate_gcode(SMALL, filament="PETG"), SMALL, filament="PETG"), "petg")
    with pytest.raises(ProcessError, match="same filament slots"):
        process_batch([(a, 1), (petg, 1)], P, LoopSettings())
    a1 = sf(make_3mf(plate_gcode(SMALL), SMALL, model_id="N2S"), "a1")
    with pytest.raises(ProcessError, match="different printer"):
        process_batch([(a, 1), (a1, 1)], P, LoopSettings())


def test_changes_lists_exactly_the_added_blocks():
    c = changes(sf(plain(BUCKET), "a"), P, LoopSettings(loops=3))
    assert [b["name"] for b in c["blocks"]] == ["START", "END"]
    assert P.door_open in c["blocks"][1]["gcode"] and c["loops"] == 3


def test_sweep_path_follows_the_written_gcode():
    a = analyze(sf(plain(BUCKET), "a"), P, LoopSettings())
    pushes = [p for p in a.path if p["push"]]
    tiers = sum(len(ln.tiers) for ps in a.plan.passes for ln in ps.lanes)
    assert len(pushes) == tiers and all(p["y"] == P.sweep.y_front for p in pushes)
    assert {p["z"] for p in pushes} == {160.0, 2.0}


def test_discovery_parses_bambu_announcements():
    msg = (
        "NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:2021\r\nNT: urn:bambulab-com:device:3dprinter:1\r\n"
        "USN: 0309CA123456789\r\nDevModel.bambu.com: N7\r\nDevName.bambu.com: Farm P2S 1\r\n\r\n"
    )
    assert parse(msg, "192.168.1.50") == {
        "serial": "0309CA123456789",
        "name": "Farm P2S 1",
        "model_id": "N7",
        "host": "192.168.1.50",
    }
    assert parse("HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nUSN: x\r\n", "1.2.3.4") is None


def test_notifications_carry_the_picture_with_a_text_fallback():
    ev = {"level": "error", "kind": "plate_not_clear", "message": "parts left", "printer": "p2s"}
    ntfy = requests_for(
        SimpleNamespace(kind="ntfy", url="https://ntfy.sh/t", token=None), "t", ev, b"\xff\xd8img"
    )
    assert (
        ntfy[0].get_method() == "PUT"
        and ntfy[0].data == b"\xff\xd8img"
        and ntfy[0].headers["Filename"] == "plate.jpg"
    )
    assert ntfy[1].get_method() == "POST" and ntfy[1].data == b"parts left"
    disc = requests_for(
        SimpleNamespace(kind="discord", url="https://d/x", token=None), "t", ev, b"\xff\xd8img"
    )
    assert (
        b'name="payload_json"' in disc[0].data
        and b"files[0]" in disc[0].data
        and b"\xff\xd8img" in disc[0].data
    )


def test_quiet_hours_window():
    q = QuietHours(enabled=True, start="22:00", end="07:00")
    assert q.until(datetime(2026, 1, 1, 23, 0)) == "07:00"
    assert q.until(datetime(2026, 1, 1, 6, 59)) == "07:00"
    assert q.until(datetime(2026, 1, 1, 12, 0)) is None
    assert QuietHours(enabled=False).until(datetime(2026, 1, 1, 23, 0)) is None
    with pytest.raises(ValueError):
        QuietHours(start="25:00")


def watch(quiet=lambda: None, results=(True, True)):
    link = Link()
    w = PrinterWatch(
        "p2s",
        link,
        Verifier(list(results)),
        Store(),
        auto_resume=True,
        quiet_until=quiet,
        settle_s=0,
        run_async=lambda f: f(),
        retry_s=0,
    )
    w.on_state(link.state)
    return w, link


def to(w, link, **kw):
    for k, v in kw.items():
        setattr(link.state, k, v)
    w.on_state(link.state)


def test_quiet_hours_hold_the_next_loop_until_morning():
    night = {"on": "07:00"}
    w, link = watch(quiet=lambda: night["on"])
    to(w, link, layer_num=850, gcode_state="PAUSE")
    assert link.resumed == 0 and w.pending_resume and "07:00" in w.status
    w.tick()
    assert link.resumed == 0
    night["on"] = None
    w.tick()  # morning: checks the plate again, then resumes
    assert link.resumed == 1 and not w.pending_resume


def test_phases_loops_measured_cooldown_and_bends():
    clock = {"t": 1000.0}
    import proofloop.monitor.watch as mon
    real = mon.time.time
    mon.time.time = lambda: clock["t"]
    try:
        w, link = watch()
        for _loop in range(2):
            to(w, link, nozzle_target=1.0, layer_num=0)
            to(w, link, nozzle_target=220.0, layer_num=5, gcode_state="RUNNING")
            clock["t"] += 3600
            to(w, link, nozzle_target=2.0, layer_num=850)
            assert w.phase == "cooling, door open"
            clock["t"] += 900  # 15 minutes of cooling
            to(w, link, nozzle_target=3.0)
            clock["t"] += 200
            to(w, link, nozzle_target=5.0)
            clock["t"] += 60
        assert w.loop_no == 2
        d = w.as_dict()
        assert d["timings"]["cooldown"]["avg"] == 900 and d["timings"]["cooldown"]["n"] == 2
        assert d["bends"]["value"] == 2
        w.reset_bends()
        assert w.as_dict()["bends"]["value"] == 0
    finally:
        mon.time.time = real


def test_batch_and_changes_api(tmp_path):
    c = TestClient(create_app(App(tmp_path, connect=False, link_factory=FakeLink)))
    t1 = up(c, plain(BUCKET), "a.gcode.3mf").json()["token"]
    t2 = up(c, plain(SMALL), "b.gcode.3mf").json()["token"]
    body = {
        "items": [{"token": t1, "count": 2}, {"token": t2, "count": 3}],
        "settings": {"wait_for_monitor": True},
    }
    pv = c.post("/api/batch/preview", json=body).json()
    assert pv["ok"] and pv["loops"] == 5 and pv["parts"] == 2 * 2 + 3 * 2
    d = c.post("/api/batch/download", json=body)
    assert d.headers["x-proofloop-filename"] == "FL_S2_batch-5x-a.gcode.3mf"
    assert threemf.loads(d.content).gcode.count("; EXECUTABLE_BLOCK_START") == 5
    ch = c.post(f"/api/plates/{t1}/changes", json={}).json()
    assert [b["name"] for b in ch["blocks"]] == ["START", "END"]
    assert (
        c.put("/api/quiet-hours", json={"enabled": True, "start": "21:30", "end": "6:00"}).json()["end"]
        == "06:00"
    )
    assert c.get("/api/state").json()["quiet_hours"]["start"] == "21:30"
    assert c.post("/api/batch/preview", json={"items": []}).status_code == 422
