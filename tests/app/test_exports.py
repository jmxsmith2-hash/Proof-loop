"""The export downloads and the invoice page."""

import csv
import io
import json
from datetime import date

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import SettingsStore
from tests.monitor.test_orders import take
from tests.monitor.test_queue import farm


def rows(r):
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"\xef\xbb\xbf") and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    return list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))


def test_the_three_downloads_are_csv_for_excel(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    take(c, price=2.0, customer="=1+1")
    app.store.add_outcome({"printer": "a", "job": "x", "started": 1.0, "ended": 2.0, "outcome": "finished", "loop_no": 0})
    got = rows(c.get("/api/orders/export.csv"))
    assert got[0]["customer"] == "'=1+1" and got[0]["parts"] == "40" and got[0]["currency"] == "$"
    assert "proofloop-orders.csv" in c.get("/api/orders/export.csv").headers["content-disposition"]
    assert rows(c.get("/api/outcomes/export.csv"))[0]["result"] == "finished"
    assert [r["file"] for r in rows(c.get("/api/costs/export.csv"))] == ["other prints (not in an order)"]
    assert c.get("/api/costs/export.csv").content.startswith(b"\xef\xbb\xbforder,customer,file,loops")
    assert c.get("/api/costs/export.csv?by=day").content.startswith(b"\xef\xbb\xbfperiod,loops")
    app.shutdown()


def test_the_date_range_filters_and_bad_dates_are_refused(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    take(c)
    today = date.today().isoformat()
    assert len(rows(c.get(f"/api/orders/export.csv?from={today}&to={today}"))) == 1
    assert rows(c.get("/api/orders/export.csv?from=2001-01-01&to=2001-12-31")) == []
    for url in ("/api/orders/export.csv?from=yesterday", "/api/outcomes/export.csv?to=31/12/2030",
                "/api/costs/export.csv?from=2030-02-01&to=2030-01-01", "/api/costs/export.csv?by=week"):
        r = c.get(url)
        assert r.status_code == 422, url
    app.shutdown()


def test_exports_need_the_access_token_like_everything_else(tmp_path):
    app = farm(tmp_path, "a")
    app.settings.app_token = "secret"
    c = TestClient(create_app(app))
    for url in ("/api/orders/export.csv", "/api/outcomes/export.csv", "/api/costs/export.csv"):
        assert c.get(url).status_code == 401
        assert c.get(url + "?token=secret").status_code == 200  # a download link can carry it
    app.shutdown()


def test_business_details_are_saved_blank_until_you_fill_them_in(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    b = c.get("/api/business").json()
    assert (b["name"], b["address"], b["email"], b["tax_id"], b["footer"]) == ("", "", "", "", "")
    assert b["prefix"] == "INV-" and b["next_number"] == 1
    r = c.put("/api/business", json={"name": "  Sam Prints ", "address": "1 Main St\nTown", "email": "a@b.c", "tax_id": "X1",
                                     "prefix": "SP-", "next_number": 12, "footer": "Pay by bank"})
    assert r.status_code == 200 and r.json()["name"] == "Sam Prints"
    assert SettingsStore(tmp_path).load().business.next_number == 12  # kept in settings.json
    assert c.put("/api/business", json={"next_number": 0}).status_code == 422
    assert c.get("/api/business").json()["prefix"] == "SP-"
    app.shutdown()


def test_an_invoice_is_a_printable_page_with_blanks_for_what_you_havent_set(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    o = take(c, quantity=40, price=2.5, customer="<b>Sam</b> & Co").json()
    r = c.get(f"/api/orders/{o['id']}/invoice")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "INV-0001" in html and date.today().isoformat() in html
    assert "&lt;b&gt;Sam&lt;/b&gt; &amp; Co" in html and "<b>Sam</b>" not in html  # a name can't add markup
    assert "$100.00" in html and "$2.50" in html and "bracket_plate" in html
    assert 'class="blank">Your business name' in html and 'class="blank">Email' in html and "@media print" in html
    assert "Sam Prints" not in html  # nothing invented
    c.put("/api/business", json={"name": "Sam Prints", "address": "1 Main St\nTown", "email": "a@b.c", "tax_id": "X1",
                                 "prefix": "INV-", "next_number": 2, "footer": "Pay by bank"})
    html = c.get(f"/api/orders/{o['id']}/invoice").text
    assert "Sam Prints" in html and "1 Main St" in html and "Tax ID: X1" in html and "Pay by bank" in html
    assert 'class="blank">Your business name' not in html and "INV-0001" in html  # the number didn't change
    app.shutdown()


def test_invoice_numbers_go_up_one_per_order_and_the_settings_follow(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    c.put("/api/business", json={"prefix": "2026-", "next_number": 40})
    a, b = take(c).json(), take(c).json()
    assert "2026-0040" in c.get(f"/api/orders/{a['id']}/invoice").text
    assert "2026-0041" in c.get(f"/api/orders/{b['id']}/invoice").text
    assert "2026-0040" in c.get(f"/api/orders/{a['id']}/invoice").text
    assert c.get("/api/business").json()["next_number"] == 42
    assert {o["id"]: o["invoice"]["label"] for o in c.get("/api/orders").json()} == {a["id"]: "2026-0040", b["id"]: "2026-0041"}
    assert c.get("/api/orders/nothing/invoice").status_code == 404
    app.shutdown()


def test_an_invoice_without_a_price_leaves_the_amounts_blank_and_shows_tax_and_paid(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    o = take(c).json()
    html = c.get(f"/api/orders/{o['id']}/invoice").text
    assert 'class="blank">price' in html and 'class="blank">amount' in html and 'class="blank">total' in html
    assert "Paid " not in html and "Includes tax" not in html

    app.settings.costs.currency = "EUR"
    app.settings.costs.tax_included_pct = 20
    c.patch(f"/api/orders/{o['id']}", json={"price_each": 3.0, "paid": True})
    html = c.get(f"/api/orders/{o['id']}/invoice").text
    assert "EUR 120.00" in html and "Includes tax (20%)" in html and "EUR 20.00" in html
    assert f"Paid {date.today().isoformat()}" in html
    app.shutdown()


def test_an_invoice_is_refused_when_orders_json_is_from_a_newer_proofloop(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    o = take(c).json()
    path = tmp_path / "orders.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**doc, "schema_version": 99}), encoding="utf-8")
    r = c.get(f"/api/orders/{o['id']}/invoice")
    assert r.status_code == 409 and "newer ProofLoop" in r.json()["detail"]
    app.shutdown()
