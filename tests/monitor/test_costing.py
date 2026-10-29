import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.monitor.costing import CostSettings, Extra, Spool, Times, cost, loops_for, match_spool
from proofloop.monitor.service import App
from tests.app.test_loop_server import FakeLink, up
from tests.plates import BUCKET, plain

SLOTS = [("1", "PLA", "#A6A9AA"), ("2", "PETG", "#000000")]
GRAMS = {"1": 50.0, "2": 10.0}
T = Times(3600, 300, 900, "measured")


def full(**kw):
    base = dict(
        spools=[
            Spool(type="PLA", color="#A6A9AA", price=20, weight_g=1000),
            Spool(type="PETG", price=30, weight_g=500),
        ],
        electricity_per_kwh=0.5,
        watts_printing=200,
        watts_idle=50,
        printer_price=1000,
        printer_life_hours=5000,
        plate_price=30,
        plate_life_loops=300,
        labor_rate=30,
        labor_min_per_loop=2,
        labor_min_per_run=12,
        overhead_per_month=120,
        printer_hours_per_month=600,
    )
    base.update(kw)
    return CostSettings(**base)


def line(r, key):
    return next(ln for ln in r.lines if ln.key == key).per_loop


def test_every_line_is_your_price_times_the_files_quantities():
    r = cost(full(), SLOTS, GRAMS, 4, T, loops=5)
    assert line(r, "material") == pytest.approx(50 / 1000 * 20 + 10 / 1000 * 60)  # PETG 30 per 500 g = 60/kg
    hours = (3600 + 300 + 900) / 3600
    assert line(r, "energy") == pytest.approx((1 * 200 + (1200 / 3600) * 50) / 1000 * 0.5, abs=1e-4)
    assert line(r, "machine") == pytest.approx(hours * 1000 / 5000, abs=1e-4)
    assert line(r, "plate") == pytest.approx(0.1)
    assert line(r, "labor") == pytest.approx(1.0)
    assert line(r, "overhead") == pytest.approx(hours * 0.2, abs=1e-4)
    assert r.setup == pytest.approx(6.0)
    assert r.totals["run"] == pytest.approx(r.totals["per_loop"] * 5 + 6, abs=1e-3)
    assert r.totals["per_part"] == pytest.approx(r.totals["run"] / 20, abs=1e-4)
    assert r.totals["complete"] and not r.missing


def test_nothing_is_guessed_missing_prices_are_listed_not_invented():
    r = cost(CostSettings(), SLOTS, GRAMS, 4, Times(3600, 300, None))
    assert all(ln.per_loop is None for ln in r.lines if ln.key in ("material", "energy", "machine", "plate"))
    assert r.totals["per_loop"] == 0 and not r.totals["complete"]
    assert any("electricity" in m for m in r.missing) and any("cooldown" in m for m in r.missing)
    assert r.capacity is None  # no capacity without a known cooldown


def test_spool_matching():
    c = full()
    assert match_spool(c, "PLA", "#a6a9aa") == 0
    assert match_spool(c, "PETG", "#123456") == 1  # the only PETG spool
    c2 = full(spools=[Spool(type="PLA", price=1), Spool(type="PLA", price=2)])
    assert match_spool(c2, "PLA", "#FFFFFF") is None  # two candidates: you choose
    c2.slot_spool["PLA|#FFFFFF"] = 1
    assert match_spool(c2, "PLA", "#ffffff") == 1


def test_failures_and_extras():
    base = cost(full(), SLOTS, GRAMS, 4, T)
    f = cost(full(failure_rate_pct=20, extras_per_part=[Extra(name="bag", cost=0.1)]), SLOTS, GRAMS, 4, T)
    assert line(f, "failures") == pytest.approx(base.totals["per_loop"] * 0.25, abs=1e-3)
    assert line(f, "extras") == pytest.approx(0.4)


def test_selling_breakeven_and_target_price():
    c = full(
        price_per_part=10,
        parts_per_order=2,
        tax_included_pct=20,
        marketplace_fee_pct=6.5,
        marketplace_fee_per_order=0.2,
        payment_fee_pct=3,
        payment_fee_per_order=0.3,
        shipping_cost_per_order=5,
        shipping_charged_per_order=4,
        extras_per_order=[Extra(name="box", cost=0.5)],
        target_margin_pct=40,
    )
    r = cost(c, SLOTS, GRAMS, 4, T)
    s = r.selling
    cpp = r.totals["per_part"]
    per_order = (0.2 + 0.3 + 5 - 4 + 0.5) / 2
    assert s["tax"] == pytest.approx(10 - 10 / 1.2, abs=1e-3)
    assert s["profit"] == pytest.approx(10 - s["tax"] - 0.95 - per_order - cpp, abs=1e-3)
    share = 1 / 1.2 - 0.095
    assert s["breakeven_price"] == pytest.approx((cpp + per_order) / share, abs=1e-3)
    assert s["price_for_target"] == pytest.approx((cpp + per_order) / (share - 0.4), abs=1e-3)
    # at the break-even price, profit really is zero
    at_be = cost(c.model_copy(update={"price_per_part": s["breakeven_price"]}), SLOTS, GRAMS, 4, T)
    assert at_be.selling["profit"] == pytest.approx(0, abs=1e-3)


def test_capacity_uses_the_real_loop_time():
    r = cost(full(price_per_part=5, printers=3, hours_per_day=20, failure_rate_pct=10), SLOTS, GRAMS, 4, T)
    loops_day = 20 * 3600 / 4800 * 3
    assert r.capacity["loops_per_day"] == pytest.approx(loops_day, abs=0.01)
    assert r.capacity["parts_per_day"] == pytest.approx(loops_day * 4 * 0.9, abs=0.1)
    assert loops_for(41, 2) == 21


def test_cost_api(tmp_path):
    c = TestClient(create_app(App(tmp_path, connect=False, link_factory=FakeLink)))
    assert c.put("/api/costs", json={"spools": [{"type": "PLA", "price": -1}]}).status_code == 422
    saved = c.put("/api/costs", json=full(price_per_part=8).model_dump()).json()
    assert saved["electricity_per_kwh"] == 0.5
    tok = up(c, plain(BUCKET)).json()["token"]
    r = c.post(f"/api/plates/{tok}/cost", json={"loops": 4, "cooldown_min": 15}).json()
    assert (
        r["loops"] == 4
        and r["times"]["cooldown_s"] == 900
        and r["times"]["cooldown_source"] == "your estimate"
    )
    assert r["parts_per_plate"] == 2 and r["selling"]["price"] == 8
    r2 = c.post(f"/api/plates/{tok}/cost", json={}).json()
    assert r2["times"]["cooldown_s"] is None and r2["capacity"] is None
    b = c.post("/api/batch/preview", json={"items": [{"token": tok, "count": 3}]}).json()
    assert b["cost"]["per_part"] > 0
