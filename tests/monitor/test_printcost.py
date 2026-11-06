"""Grams and cost of every finished print, and the print history API."""

import time

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.monitor.costing import CostSettings
from proofloop.monitor.filament import Spool
from proofloop.monitor.printcost import history, price_outcome
from tests.monitor.test_queue import farm
from tests.monitor.test_journal import watched
from tests.monitor.test_monitor import to


def made(app, name="Boot", grams=50.0, layers=None):
    """A loop file ProofLoop made: one plate, one PLA slot."""
    plate = {"name": name, "slots": [["1", "PLA", "#FFFFFF"]], "grams": {"1": grams}, "parts": 4, "layers": layers}
    app.store.put_job(name, {"file": name, "profile": "x", "loops": 1, "plates": [plate], "order": [0]})


def outcome(app, job, result="finished", loop_no=0, seconds=3600, **extra):
    now = time.time()
    rec = {"printer": "a", "job": job, "loop_no": loop_no, "loop_file": False, "started": now - seconds,
           "ended": now, "outcome": result, "layer": 100, "total_layers": 100, **extra}
    rec["id"] = app.store.add_outcome(rec)
    rec["ts"] = now
    return rec


def priced_app(tmp_path):
    app = farm(tmp_path, "a")
    app.inventory.save(Spool(material="PLA", color="#FFFFFF", price=20.0, net_g=1000))   # $20 a kg
    app.settings.costs = CostSettings(currency="$", electricity_per_kwh=0.5, watts_printing=200)
    return app


def test_a_file_proofloop_made_gets_grams_and_a_cost_from_your_prices(tmp_path):
    app = priced_app(tmp_path)
    made(app, "Boot", 50.0)
    p = price_outcome(app, outcome(app, "Boot"))
    assert p["grams"] == 50.0 and p["filaments"] == ["PLA"] and p["grams_source"] == "slicer"
    assert p["material_cost"] == 1.0                                   # 50 g at $20/kg
    assert abs(p["cost"] - (1.0 + 0.2 * 0.5)) < 1e-6                   # + 1 h at 200 W, $0.50/kWh
    assert p["by_type"] == {"PLA": {"grams": 50.0, "material_cost": 1.0}}
    assert p["seconds"] == 3600
    app.shutdown()


def test_a_file_proofloop_didnt_make_is_unknown_not_guessed(tmp_path):
    app = priced_app(tmp_path)
    p = price_outcome(app, outcome(app, "benchy", filaments=[{"tray": 0, "type": "pla", "color": "#fff"}]))
    assert p["grams"] is None and p["cost"] is None and p["filaments"] == ["PLA"]
    assert "isn't known" in p["note"]
    app.shutdown()


def test_a_stopped_print_is_charged_for_what_it_printed(tmp_path):
    app = priced_app(tmp_path)
    made(app, "Boot", 100.0)
    half = price_outcome(app, outcome(app, "Boot", "failed", layer=25, total_layers=100))
    assert half["grams"] == 25.0 and "estimated" in half["grams_source"]
    layers = {"layers": 4, "grams": {"1": [10.0, 20.0, 30.0, 100.0]}}
    made(app, "Cup", 100.0, layers)
    exact = price_outcome(app, outcome(app, "Cup", "cancelled", layer=2, total_layers=4))
    assert exact["grams"] == 20.0 and "G-code" in exact["grams_source"]
    app.shutdown()


def test_unpriced_filament_leaves_the_cost_a_minimum(tmp_path):
    app = farm(tmp_path, "a")
    made(app, "Boot", 50.0)
    p = price_outcome(app, outcome(app, "Boot"))
    assert p["grams"] == 50.0 and p["material_cost"] is None and p["cost"] is None and not p["cost_complete"]
    app.shutdown()


def test_the_history_totals_by_type_leave_out_unknown_grams(tmp_path):
    app = priced_app(tmp_path)
    made(app, "Boot", 50.0)
    outcome(app, "Boot")
    outcome(app, "Boot", loop_no=2)
    outcome(app, "benchy")
    h = history(app)
    assert [r["job"] for r in h["rows"]] == ["benchy", "Boot", "Boot"]        # newest first
    m = h["month"]
    assert m["prints"] == 3 and m["grams"] == 100.0 and m["unknown_grams"] == 1
    assert m["by_type"] == [{"type": "PLA", "prints": 2, "grams": 100.0, "material_cost": 2.0}]
    assert "1 print with unknown filament is left out" in m["note"]
    unknown = h["rows"][0]
    assert unknown["grams"] is None and unknown["grams_known"] is False and unknown["cost"] is None
    # what a reprint button needs
    assert {"id", "job", "file", "printer", "has_file", "loop_no"} <= set(h["rows"][1])
    app.shutdown()


def test_the_history_endpoint_and_its_filters(tmp_path):
    app = priced_app(tmp_path)
    made(app, "Boot", 50.0)
    outcome(app, "Boot")
    c = TestClient(create_app(app))
    h = c.get("/api/history").json()
    assert h["currency"] == "$" and h["rows"][0]["grams"] == 50.0 and h["rows"][0]["printer"] == "a"
    assert c.get("/api/history?printer=nope").json()["rows"] == []
    assert c.get("/api/history?limit=0").status_code == 200
    app.shutdown()


def test_a_print_is_priced_when_it_ends_and_keeps_that_price(tmp_path):
    w, link, store, journal = watched(tmp_path, job="Boot")
    app = priced_app(tmp_path)
    made(app, "Boot", 50.0)
    journal.pricer = lambda rec, trays: price_outcome(app, rec, trays)
    to(w, link, gcode_state="RUNNING", layer_num=10)
    to(w, link, layer_num=120, gcode_state="FINISH")
    (rec,) = store.outcomes()
    assert rec["costing"]["grams"] == 50.0 and rec["costing"]["material_cost"] == 1.0
    app.store.add_outcome(rec)                                           # the same record in the app's store
    app.settings.costs = CostSettings(currency="€")                      # later price changes don't rewrite it
    row = history(app)["rows"][0]
    assert row["recorded"] is True and row["material_cost"] == 1.0
    app.shutdown()


def test_a_pricing_error_never_loses_the_record(tmp_path):
    w, link, store, journal = watched(tmp_path, job="Boot")

    def broken(rec, trays):
        raise ValueError("boom")

    journal.pricer = broken
    to(w, link, gcode_state="RUNNING", layer_num=10)
    to(w, link, layer_num=120, gcode_state="FINISH")
    (rec,) = store.outcomes()
    assert rec["outcome"] == "finished" and "costing" not in rec
