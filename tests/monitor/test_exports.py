"""CSV exports: safe for Excel, no formulas from names, plain numbers, the right rows for a date range."""

import csv
import io
import time
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.monitor import exports
from tests.monitor.test_orders import take
from tests.monitor.test_queue import farm


def ts(y, m, d, h=12):
    return datetime(y, m, d, h).timestamp()


def parse(raw: bytes) -> list[dict]:
    assert raw.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


def production(app, printer, job, when, parts=2, grams=10.0, ok=True):
    app.store._db.execute("INSERT INTO production (ts, printer, job, loop_no, parts, grams, ok) VALUES (?,?,?,?,?,?,?)",
                          (when, printer, job, 1, parts, grams, int(ok)))


def test_a_cell_that_could_be_a_formula_is_quoted():
    for text in ("=1+1", "+1", "-2", "@SUM(A1)", "\t=1", '=HYPERLINK("http://x")'):
        assert exports.safe(text) == "'" + text
    assert exports.safe("Sam's shop") == "Sam's shop" and exports.safe("") == "" and exports.safe(None) is None
    assert exports.safe(-5) == -5 and exports.safe(2.5) == 2.5  # numbers are numbers, not text


def test_the_csv_is_utf8_with_a_bom_and_keeps_numbers_plain():
    raw = exports.to_csv(["customer", "price"], [{"customer": 'Zoë, "Ltd"', "price": -3.5}, {"customer": "=2+2"}])
    assert raw[:3] == b"\xef\xbb\xbf"
    assert parse(raw) == [{"customer": 'Zoë, "Ltd"', "price": "-3.5"}, {"customer": "'=2+2", "price": ""}]


def test_a_date_range_includes_both_days_and_rejects_nonsense():
    since, until = exports.date_range("2026-10-01", "2026-10-31")
    assert since == ts(2026, 10, 1, 0) and until == ts(2026, 11, 1, 0)
    assert exports.date_range(None, None) == (0.0, float("inf"))
    assert exports.date_range("2026-10-05", None)[1] == float("inf")
    for bad in (("10/05/2026", None), (None, "tomorrow"), ("2026-10-31", "2026-10-01")):
        with pytest.raises(ValueError):
            exports.date_range(*bad)


def test_orders_rows_say_what_is_made_when_and_what_it_earns(tmp_path):
    app = farm(tmp_path, "a", "b")
    c = TestClient(create_app(app))
    due = date.today().replace(year=date.today().year + 1).isoformat()
    o = take(c, quantity=40, due=due, price=2.5, customer="=cmd|'/C calc'!A0").json()
    app.orders.update(o["id"], cost_each=0.5)
    row = exports.orders_rows(app)[0]
    assert row["customer"] == "=cmd|'/C calc'!A0" and row["parts"] == 40 and row["made"] == 0
    assert row["status"] == "open" and row["due"] == due and row["on_time"] == "yes" and row["ready_by"]
    assert (row["price_each"], row["value"], row["earned"], row["cost"], row["profit"]) == (2.5, 100.0, 0.0, 20.0, 80.0)
    assert row["currency"] == "$" and row["paid"] == "no" and row["paid_on"] == "" and row["invoice"] == ""
    assert parse(exports.to_csv(exports.ORDER_COLUMNS, [row]))[0]["customer"].startswith("'=cmd")

    c.patch(f"/api/orders/{o['id']}", json={"status": "done", "paid": True})
    row = exports.orders_rows(app)[0]
    assert row["earned"] == 100.0 and row["paid"] == "yes" and row["paid_on"] == date.today().isoformat()

    c.patch(f"/api/orders/{o['id']}", json={"status": "cancelled"})
    row = exports.orders_rows(app)[0]
    assert row["earned"] == 0.0 and row["value"] is None and row["profit"] is None and row["on_time"] == ""
    assert exports.orders_rows(app, *exports.date_range("2001-01-01", "2001-12-31")) == []
    app.shutdown()


def test_an_order_without_a_price_has_blanks_not_zeros(tmp_path):
    app = farm(tmp_path, "a")
    take(TestClient(create_app(app)))
    row = exports.orders_rows(app)[0]
    assert row["price_each"] is None and row["value"] is None and row["earned"] is None
    app.shutdown()


def test_history_rows_are_oldest_first_with_the_reason_a_print_stopped(tmp_path):
    app = farm(tmp_path, "a")
    base = {"printer": "a", "job": "bracket", "loop_no": 3, "loop_file": True, "layer": 40, "total_layers": 100}
    app.store.add_outcome({**base, "started": ts(2026, 10, 2, 8), "ended": ts(2026, 10, 2, 9), "outcome": "failed",
                           "print_error": "0500C011", "hms": [{"code": "0500_0300", "level": 2}], "eject": "parts_left"})
    app.store.add_outcome({**base, "loop_no": 1, "started": ts(2026, 10, 1, 8), "ended": ts(2026, 10, 1, 9),
                           "outcome": "finished", "layer": 100, "print_error": None, "hms": []})
    app.store.add_outcome({**base, "loop_file": False, "started": ts(2026, 10, 3, 8), "ended": ts(2026, 10, 3, 9),
                           "outcome": "cancelled", "print_error": "0300400C", "hms": []})
    rows = exports.history_rows(app)
    assert [r["result"] for r in rows] == ["finished", "failed", "cancelled"]
    assert rows[0]["ended"] == "2026-10-01 09:00:00" and rows[0]["loop"] == 1 and rows[0]["reason"] == ""
    assert rows[1]["reason"] == "error 0500C011; HMS 0500_0300" and rows[1]["plate_check"] == "parts left"
    assert rows[1]["layer"] == 40 and rows[1]["total_layers"] == 100
    assert rows[2]["reason"] == "stopped at the printer" and rows[2]["loop"] == ""
    only = exports.history_rows(app, *exports.date_range("2026-10-02", "2026-10-02"))
    assert [r["result"] for r in only] == ["failed"]
    app.shutdown()


def test_costs_per_order_add_up_the_loops_that_made_its_parts(tmp_path):
    app = farm(tmp_path, "a", "b")
    c = TestClient(create_app(app))
    o = take(c, quantity=40, price=2.5, customer="Sam").json()
    app.orders.update(o["id"], cost_each=0.5)
    run = app.queue.get(o["run"])
    job = next(j for j in run["jobs"] if j["printer"] == "a")
    app.queue.mark(run["id"], job["id"], status="done", file="sam-plate.gcode.3mf", at=1.0)
    production(app, "a", "sam-plate", ts(2026, 10, 5), parts=2, grams=11.5)
    production(app, "a", "sam-plate", ts(2026, 10, 6), parts=2, grams=11.5)
    production(app, "a", "sam-plate", ts(2026, 10, 6), ok=False, parts=0, grams=0)
    production(app, "b", "someone-elses", ts(2026, 10, 6), parts=4, grams=30)  # not in any order
    app.store.add_outcome({"printer": "a", "job": "sam-plate", "started": ts(2026, 10, 5, 8),
                           "ended": ts(2026, 10, 5, 10), "outcome": "finished"})
    rows = exports.costs_rows(app)
    mine, other = rows
    assert (mine["order"], mine["customer"], mine["loops"], mine["failed_loops"], mine["parts"]) == (o["id"], "Sam", 2, 1, 4)
    assert (mine["grams"], mine["print_hours"]) == (23.0, 2.0)
    assert (mine["cost"], mine["price"], mine["profit"], mine["margin_pct"]) == (2.0, 10.0, 8.0, 80.0)
    assert other["order"] == "" and other["parts"] == 4 and other["grams"] == 30.0
    assert other["cost"] is None and other["price"] is None and other["margin_pct"] is None
    assert exports.costs_rows(app, *exports.date_range("2026-10-06", "2026-10-06"))[0]["parts"] == 2
    assert parse(exports.to_csv(exports.cost_columns("order"), rows))[0]["margin_pct"] == "80.0"
    app.shutdown()


def test_costs_per_day_and_per_month_price_only_the_parts_in_orders(tmp_path):
    app = farm(tmp_path, "a")
    o = take(TestClient(create_app(app)), quantity=40, price=2.0).json()
    app.orders.update(o["id"], cost_each=None)  # no cost per part given
    run = app.queue.get(o["run"])
    app.queue.mark(run["id"], run["jobs"][0]["id"], status="done", file="p.gcode.3mf", at=1.0)
    production(app, "a", "p", ts(2026, 10, 5), parts=2)
    production(app, "a", "p", ts(2026, 11, 1), parts=2)
    production(app, "a", "q", ts(2026, 11, 1), parts=3)
    days = exports.costs_rows(app, by="day")
    assert [(r["period"], r["parts"], r["order_parts"]) for r in days] == [("2026-10-05", 2, 2), ("2026-11-01", 5, 2)]
    assert days[0]["price"] == 4.0 and days[0]["cost"] is None and days[0]["profit"] is None  # no cost: no profit
    months = exports.costs_rows(app, by="month")
    assert [r["period"] for r in months] == ["2026-10", "2026-11"] and months[1]["loops"] == 2
    with pytest.raises(ValueError):
        exports.costs_rows(app, by="week")
    app.shutdown()


def test_two_orders_claiming_one_file_are_not_guessed_at(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    for _ in range(2):
        o = take(c, price=1).json()
        run = app.queue.get(o["run"])
        app.queue.mark(run["id"], run["jobs"][0]["id"], status="done", file="same.gcode.3mf", at=1.0)
    production(app, "a", "same", time.time())
    rows = exports.costs_rows(app)
    assert len(rows) == 1 and rows[0]["order"] == "" and rows[0]["parts"] == 2
    app.shutdown()
