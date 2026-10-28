"""Batches: different plates combined in one loop file."""

import io
import re
import types

import pytest
from fastapi.testclient import TestClient
from fixtures import BUCKET, SMALL, farmloop_version, make_3mf, plain, plate_gcode
from test_server import FakeLink

from clearloop import threemf
from clearloop.config import PrinterConfig
from clearloop.filament import Spool
from clearloop.gcode.processor import process, process_batch
from clearloop.gcode.routines import LoopSettings
from clearloop.lint import lint
from clearloop.profiles import ProfileStore
from clearloop.server import create_app
from clearloop.service import App
from clearloop.vision import BedCheck

P = ProfileStore().get("p2s-stage2-farmboard")


def loops_of(text: str) -> list[str]:
    return text.split("; === CLEARLOOP LOOP ")


def test_each_plate_keeps_its_own_header_sweep_and_push_heights():
    a = threemf.loads(plain(BUCKET), name="a.gcode.3mf")
    b = threemf.loads(plain(SMALL), name="b.gcode.3mf")
    # part "1" of plate B gets its own height; plate A has no part "1", so it's unaffected
    r = process_batch([(a, 2), (b, 2, {"1": 6.0})], P, LoopSettings(wait_for_monitor=True))
    bodies = loops_of(threemf.loads(r.data).gcode)
    assert [re.search(r"; total layer number: (\d+)", x).group(1) for x in bodies] == [
        "850",
        "850",
        "60",
        "60",
    ]
    assert [x.count("M400 U1") for x in bodies] == [1, 1, 1, 0]  # a plate check after every loop but the last
    cycle = [x[x.rfind("; MACHINE_END_GCODE_START") :] for x in bodies]  # the ejection cycle of each loop
    zs = [set(re.findall(r"^G1 Z([\d.]+)", x, re.M)) for x in cycle]
    assert "6.00" in zs[2] and "6.00" not in zs[0]  # plate B's own push height, only on plate B
    assert "160.00" in zs[0] and "160.00" not in zs[2]  # plate A's tall part, only on plate A


def test_the_checker_checks_every_plate_against_its_own_sweep():
    good = process(threemf.loads(plain(SMALL)), P, LoopSettings()).gcode
    bad = farmloop_version(plate_gcode(BUCKET))  # one push height: misses the 5 mm clip
    sf = threemf.loads(make_3mf(good + "\n" + bad, SMALL))
    f = {x.title: x.detail for x in lint(sf)}
    assert "loop 2: " in f["Parts the sweep passes over"] and "5.0 mm" in f["Parts the sweep passes over"]
    ok = threemf.loads(
        process_batch(
            [(threemf.loads(plain(BUCKET)), 1), (threemf.loads(plain(SMALL)), 2)], P, LoopSettings()
        ).data
    )
    f = {x.title: x.detail for x in lint(ok)}
    assert "across 2 different plates" in f["Every part is reached by a sweep pass"]


@pytest.fixture
def clock(monkeypatch):
    import clearloop.monitor as mon

    now = [1_800_000_000.0]

    def tick():
        now[0] += 120  # two minutes between printer reports
        return now[0]

    monkeypatch.setattr(mon, "time", types.SimpleNamespace(time=tick, sleep=lambda s: None))


@pytest.mark.parametrize("stage", [2, 1])
@pytest.mark.parametrize("printer_total", ["per plate", "first plate"])
def test_monitor_follows_every_plate_of_a_batch(tmp_path, clock, stage, printer_total):
    """Where the printer gets its layer total isn't documented, so both behaviours must work."""
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    app.add_printer(PrinterConfig(name="p2s", host="1", serial="S", access_code="x", model_id="N7"))
    c = TestClient(create_app(app))
    up = lambda data, n: c.post("/api/plates", files={"file": (n, io.BytesIO(data))}).json()["token"]  # noqa: E731
    ta, tb = (
        up(plain(BUCKET), "a.gcode.3mf"),
        up(make_3mf(plate_gcode(SMALL), SMALL, grams=4.0), "b.gcode.3mf"),
    )
    prof = "p2s-stage2-farmboard" if stage == 2 else "p2s-sweep-only"
    body = {
        "items": [{"token": ta, "count": 2}, {"token": tb, "count": 3}],
        "settings": {"wait_for_monitor": True, "profile_id": prof},
    }
    name = c.post("/api/batch/download", json=body).headers["x-clearloop-filename"]
    sp = app.inventory.save(Spool(material="PLA", color="#FFFFFF", net_g=1000), sealed=True)

    w = app.watches["p2s"]
    st = w.link.state
    resumed = []
    w.link.resume = lambda: resumed.append(w.loop_no) or True
    w.verifier = types.SimpleNamespace(
        references=["ref"], dir=tmp_path, check=lambda jpeg, want_diff=True: BedCheck(True, 0.0, 0, "ref")
    )
    w.run_async, w.settle_s = (lambda f: f()), 0
    st.subtask_name = name.removesuffix(".gcode.3mf")
    plates = [850, 850, 60, 60, 60]
    for i, layers in enumerate(plates, 1):
        st.gcode_state = "RUNNING"
        if stage == 2:
            st.nozzle_target = 1.0
            w.on_state(st)
        st.total_layers = layers if printer_total == "per plate" else 850
        for layer in (1, layers // 2, layers):
            st.layer_num = layer
            w.on_state(st)
        for t in (2.0, 3.0, 4.0, 5.0) if stage == 2 else ():
            st.nozzle_target = t
            w.on_state(st)
        st.gcode_state = "PAUSE" if i < len(plates) else "FINISH"
        w.on_state(st)
    assert w.loop_no == 5 and resumed == [1, 2, 3, 4]
    assert app.inventory.get(sp.id).remaining_g == pytest.approx(1000 - 2 * 12.5 - 3 * 4.0)
    assert [r["parts"] for r in app.store.production()] == [2, 2, 2, 2, 2]


def test_batch_preview_explains_what_the_printer_shows(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    c = TestClient(create_app(app))
    up = lambda data, n: c.post("/api/plates", files={"file": (n, io.BytesIO(data))}).json()["token"]  # noqa: E731
    shared = [("93", 30.0, 107.0, 60.0, 162.0, 20.0), ("2", 170.0, 60.0, 190.0, 80.0, 4.0)]
    ta, tb = up(plain(BUCKET), "a.gcode.3mf"), up(plain(shared), "b.gcode.3mf")
    r = c.post(
        "/api/batch/preview",
        json={"items": [{"token": ta, "count": 1}, {"token": tb, "count": 1, "part_push": {"93": 9}}]},
    ).json()
    assert r["ok"] and r["items"][1]["custom_heights"] == 1
    assert any("first plate's picture" in n for n in r["notes"])
    assert any("skip object" in n and "93" in n for n in r["notes"])
    bad = c.post("/api/batch/preview", json={"items": [{"token": ta, "count": 1, "part_push": {"93": -4}}]})
    assert bad.status_code == 422
