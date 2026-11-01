"""The run dashboard: parts per hour counted from the printers, each open order's finish, each printer's next job."""

import time
from datetime import date, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.monitor import store as store_mod
from proofloop.monitor.dashboard import dashboard, pace, rate
from tests.monitor.test_orders import take
from tests.monitor.test_queue import farm

H = 3600.0


def loops(app, monkeypatch, printer, *at, parts=4, ok=True, job="FL_S2_bracket"):
    """Loops that ended on ``printer`` at each of the times ``at``."""
    for i, ts in enumerate(at, 1):
        monkeypatch.setattr(store_mod, "time", SimpleNamespace(time=lambda ts=ts: ts))
        app.store.add_production(printer, job, i, parts if ok else 0, 20.0, ok=ok)
    monkeypatch.setattr(store_mod, "time", time)


def test_parts_per_hour_are_counted_over_the_period(tmp_path, monkeypatch):
    app = farm(tmp_path, "a", "b", "c")
    now = time.time()
    loops(app, monkeypatch, "a", *(now - 10 * 24 * H + i * H for i in range(3)))  # 10 days ago: counting began then
    loops(app, monkeypatch, "a", *(now - k * H for k in (20, 12, 6, 1)))  # 4 loops of 4 parts in the last day
    loops(app, monkeypatch, "a", now - 3 * 24 * H)
    loops(app, monkeypatch, "a", now - 2 * H, ok=False)
    d = dashboard(app, now)
    a = next(p for p in d["printers"] if p["printer"] == "a")
    assert a["rate"]["24h"] == {"per_hour": round(16 / 24, 2), "parts": 16, "loops": 4, "failed": 1,
                                "uncounted": 0, "hours": 24.0}
    assert a["rate"]["7d"]["parts"] == 20 and a["rate"]["7d"]["per_hour"] == round(20 / 168, 2)
    c = next(p for p in d["printers"] if p["printer"] == "c")  # nothing counted: no number, not a zero
    assert c["rate"]["24h"]["per_hour"] is None and c["rate"]["7d"]["per_hour"] is None and c["pace"] is None
    assert d["farm"] == {"24h": a["rate"]["24h"]["per_hour"], "7d": a["rate"]["7d"]["per_hour"]}
    app.shutdown()


def test_a_new_printer_is_counted_from_its_first_loop(tmp_path, monkeypatch):
    app = farm(tmp_path, "b")
    now = time.time()
    loops(app, monkeypatch, "b", now - 10 * H, now - 8 * H, now - 6 * H, now - 4 * H, now - 2 * H)
    r = dashboard(app, now)["printers"][0]["rate"]
    # the first loop's start isn't known: its parts are left out and the hours start at its end
    assert r["7d"] == r["24h"] == {"per_hour": 1.6, "parts": 16, "loops": 4, "failed": 0, "uncounted": 0,
                                   "hours": 10.0}
    app.shutdown()


def test_not_enough_data_is_no_number():
    now = 1_000_000.0
    first = now - 1800  # half an hour counted
    assert rate([{"ts": now - 60, "parts": 4, "ok": 1}], first, now, 24)["per_hour"] is None
    # loops of files not made by this ProofLoop: their parts aren't known
    other = [{"ts": now - k * H, "parts": 0, "ok": 1} for k in (1, 2)]
    assert rate(other, now - 30 * H, now, 24)["per_hour"] is None
    assert rate(other, now - 30 * H, now, 24)["uncounted"] == 2
    failed = [{"ts": now - H, "parts": 0, "ok": 0}]
    assert rate(failed, now - 30 * H, now, 24)["per_hour"] == 0.0  # it printed, nothing came of it
    assert rate([], None, now, 24)["per_hour"] is None


def test_the_pace_while_looping_comes_from_measured_loop_times():
    rows = [{"ts": i, "parts": p, "ok": 1} for i, p in enumerate((2, 2, 4, 4))] + [{"ts": 9, "parts": 0, "ok": 0}]
    assert pace(rows, None) is None  # no loop timed yet
    assert pace(rows, {"avg": 1800.0, "n": 2, "last": 1800.0}) == {"per_hour": 8.0, "loops": 2, "loop_s": 1800}
    assert pace([], {"avg": 1800.0, "n": 2, "last": 1800.0}) is None


def test_orders_show_parts_to_make_and_when_theyll_be_done(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    soon = take(c, quantity=400, due=date.today().isoformat(), customer="Late Ltd").json()
    later = take(c, quantity=8, due=(date.today() + timedelta(days=60)).isoformat()).json()
    undated = take(c, quantity=4, customer="Whenever").json()
    d = c.get("/api/dashboard").json()
    assert [o["id"] for o in d["orders"]] == [soon["id"], later["id"], undated["id"]]  # the soonest due first
    late = d["orders"][0]
    assert late["made"] == 0 and late["left"] == 400 and late["on_time"] is False and late["late_by_days"] >= 1
    assert late["eta_from"] == "planned" and late["printers"] == ["a"]  # a hasn't timed a loop yet
    app.store.add_timing("a", "loop", 1200)
    assert dashboard(app)["orders"][0]["eta_from"] == "measured"
    app.orders.update(undated["id"], status="cancelled")
    assert undated["id"] not in [o["id"] for o in dashboard(app)["orders"]]  # open orders only
    app.shutdown()


def test_each_printer_says_what_its_on_and_whats_next(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    first = take(c, quantity=8).json()
    take(c, quantity=4, customer="Pat")
    p = dashboard(app)["printers"][0]
    assert p["now"] is None and p["next"]["order"] == "Sam's shop" and p["waiting_jobs"] == 2
    assert p["hands_off"] is False and p["hands_off_on"] is False and p["stopped"] == 0
    run = app.queue.get(first["run"])
    app.queue.mark(run["id"], run["jobs"][0]["id"], status="printing", loop=2)
    app.settings.printers[0].hands_off = True
    p = dashboard(app)["printers"][0]
    assert p["now"]["status"] == "printing" and p["now"]["loop"] == 2 and p["now"]["order"] == "Sam's shop"
    assert p["next"]["order"] == "Pat" and p["waiting_jobs"] == 1
    assert p["hands_off"] is True and p["hands_off_on"] is False  # Sending to printers is off
    app.settings.send_to_printer = True
    assert dashboard(app)["printers"][0]["hands_off_on"] is True
    app.shutdown()
