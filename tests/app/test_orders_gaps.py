"""Taking an order never leaves a queued run nobody follows; paid orders and the customer table."""

import json

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from tests.monitor.test_orders import take
from tests.monitor.test_queue import farm


def newer_orders_file(tmp_path):
    path = tmp_path / "orders.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"orders": []}
    path.write_text(json.dumps({**doc, "schema_version": 99}), encoding="utf-8")


def test_a_newer_orders_file_is_refused_before_anything_is_queued(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    newer_orders_file(tmp_path)
    r = take(c, price=2.0)
    assert r.status_code == 409 and "newer ProofLoop" in r.json()["detail"]
    assert app.queue.runs() == []  # no orphaned run
    assert json.loads((tmp_path / "orders.json").read_text(encoding="utf-8"))["schema_version"] == 99
    app.shutdown()


def test_a_run_is_taken_out_of_the_queue_when_the_order_cant_be_saved(tmp_path, monkeypatch):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app), raise_server_exceptions=False)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(app.orders, "add", fail)
    assert take(c).status_code == 500
    assert app.queue.runs() == [] and list(app.queue.folder.glob("*.gcode.3mf")) == []
    monkeypatch.undo()
    assert take(c).status_code == 200 and len(app.queue.runs()) == 1  # and it works again
    app.shutdown()


def test_changing_or_removing_an_order_says_so_when_the_file_is_newer(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    o = take(c).json()
    newer_orders_file(tmp_path)
    r = c.patch(f"/api/orders/{o['id']}", json={"notes": "x"})
    assert r.status_code == 409 and "newer ProofLoop" in r.json()["detail"]
    assert c.delete(f"/api/orders/{o['id']}").status_code == 409
    app.shutdown()


def test_marking_an_order_paid_and_unpaid(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    o = take(c, price=2.0).json()
    assert not o.get("paid")
    p = c.patch(f"/api/orders/{o['id']}", json={"paid": True}).json()
    assert p["paid"] is True and p["paid_at"] > 0
    p = c.patch(f"/api/orders/{o['id']}", json={"paid": False}).json()
    assert p["paid"] is False and p["paid_at"] is None
    assert c.patch(f"/api/orders/{o['id']}", json={"paid": "yes"}).status_code == 422
    app.shutdown()


def test_the_customer_table(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    assert c.get("/api/orders/customers").json() == []
    a = take(c, price=2.0, customer="Sam").json()
    take(c, price=1.0, customer="sam", quantity=10)
    take(c, customer="Ann")
    c.patch(f"/api/orders/{a['id']}", json={"status": "done"})
    got = {x["customer"]: x for x in c.get("/api/orders/customers").json()}
    assert got["Sam"] == {"customer": "Sam", "orders": 2, "parts": 50, "earned": 80.0, "unpaid": 80.0}
    c.patch(f"/api/orders/{a['id']}", json={"paid": True})
    assert {x["customer"]: x for x in c.get("/api/orders/customers").json()}["Sam"]["unpaid"] == 0.0
    assert got["Ann"]["orders"] == 1 and got["Ann"]["earned"] == 0.0
    app.shutdown()
