"""A quote from a file on the Costs tab: a model is sliced for the printer (nothing made or sent), a sliced plate is
read as it is, and the suggested price is cost x (1 + markup)."""

import math

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import SliceUsual
from proofloop.monitor.costing import CostSettings, Times, cost
from proofloop.monitor.service import App
from tests.check.test_prepare import MACHINE, profiles  # noqa: F401  (the preset tree and the stand-in slicer)
from tests.loopfiles.test_slicing import PLA, PROCESS, _cube, lib  # noqa: F401

WINDOW = {"X-ProofLoop": "1", "Origin": "http://127.0.0.1:8600"}
RATES = dict(electricity_per_kwh=0.5, watts_printing=200, watts_idle=50, printer_price=1000, printer_life_hours=5000)


@pytest.fixture()
def app(lib, tmp_path):  # noqa: F811
    a = App(tmp_path / "data", connect=False)
    a.settings.slice_usual = SliceUsual(machine=MACHINE, process=PROCESS, filament=PLA, plate="Textured PEI Plate")
    a.settings.costs = CostSettings(**RATES)
    return a


def client(app):
    return TestClient(create_app(app), base_url="http://127.0.0.1:8600")


def test_the_markup_sets_the_suggested_price():
    t = Times(3600, 300, 900, "measured")
    r = cost(CostSettings(**RATES), [("1", "PLA", "#A6A9AA")], {"1": 50.0}, 4, t, loops=2)
    cpp = r.quote["cost_per_part"]
    assert r.quote["markup_pct"] == 100  # the default: sell at double the cost
    assert r.quote["markup_price"] == pytest.approx(cpp * 2, abs=0.01)
    half = cost(CostSettings(**RATES, markup_pct=25), [("1", "PLA", "#A6A9AA")], {"1": 50.0}, 4, t, loops=2)
    assert half.quote["markup_price"] == pytest.approx(half.quote["cost_per_part"] * 1.25, abs=0.01)
    none = cost(CostSettings(**RATES, markup_pct=None), [("1", "PLA", "#A6A9AA")], {"1": 50.0}, 4, t, loops=2)
    assert none.quote["markup_price"] is None


def test_a_model_is_quoted_for_its_copies_and_the_markup_follows_the_settings(app):
    c = client(app)
    model = c.post("/api/plates", files={"file": ("cube.stl", _cube())}, headers=WINDOW).json()["model"]
    plate = c.post(f"/api/models/{model['token']}/slice-for-order", json={"quantity": 30}, headers=WINDOW).json()
    assert plate["token"] and plate["parts"]
    per_plate = len(plate["parts"])
    loops = math.ceil(30 / per_plate)
    q = c.post(f"/api/plates/{plate['token']}/cost", json={"loops": loops, "cooldown_min": 10}, headers=WINDOW).json()
    assert q["loops"] == loops and q["quote"]["markup_pct"] == 100
    assert q["quote"]["cost_per_part"] > 0
    assert q["quote"]["markup_price"] == pytest.approx(q["quote"]["cost_per_part"] * 2, abs=0.01)
    assert q["times"]["print_s"] > 0 and q["materials"][0]["grams"] > 0

    # the markup is a setting: change it and the same plate is quoted again
    c.put("/api/costs", json={**app.settings.costs.model_dump(), "markup_pct": 50}, headers=WINDOW)
    again = c.post(f"/api/plates/{plate['token']}/cost", json={"loops": loops, "cooldown_min": 10}, headers=WINDOW).json()
    assert again["quote"]["markup_pct"] == 50
    assert again["quote"]["markup_price"] == pytest.approx(again["quote"]["cost_per_part"] * 1.5, abs=0.01)


def test_a_sliced_file_is_quoted_with_its_own_time_and_grams(app):
    c = client(app)
    model = c.post("/api/plates", files={"file": ("cube.stl", _cube())}, headers=WINDOW).json()["model"]
    sliced = c.post(f"/api/models/{model['token']}/slice-for-order", json={"quantity": 1}, headers=WINDOW).json()
    made = c.post(f"/api/plates/{sliced['token']}/download", json={}, headers=WINDOW)
    assert made.status_code == 200
    again = c.post("/api/plates", files={"file": ("cube.gcode.3mf", made.content)}, headers=WINDOW).json()
    assert "model" not in again and again["token"]  # a sliced plate opens as it is: nothing to slice
    cost_of = lambda tok: c.post(f"/api/plates/{tok}/cost", json={"loops": 1, "cooldown_min": 10}, headers=WINDOW).json()
    own, original = cost_of(again["token"]), cost_of(sliced["token"])
    assert own["times"]["print_s"] == original["times"]["print_s"]
    assert own["materials"][0]["grams"] == pytest.approx(original["materials"][0]["grams"])
    assert own["quote"]["markup_price"] == original["quote"]["markup_price"]


def test_quoting_a_model_leaves_the_usual_slice_alone(app):
    c = client(app)
    before = app.settings.slice_usual.model_copy()
    model = c.post("/api/plates", files={"file": ("cube.stl", _cube())}, headers=WINDOW).json()["model"]
    c.post(f"/api/models/{model['token']}/slice-for-order", json={"quantity": 5}, headers=WINDOW)
    assert app.settings.slice_usual == before
