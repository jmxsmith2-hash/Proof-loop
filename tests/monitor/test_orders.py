"""Orders: planned into the queue, followed as their jobs print, on time or not, and what they earn."""

import time
from datetime import date, timedelta

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.monitor.orders import due_ts, progress
from proofloop.monitor.summary import summary
from tests.monitor.test_queue import PLATE, farm


def take(c, quantity=40, due=None, price=None, customer="Sam's shop"):
    token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
    return c.post("/api/orders", json={"token": token, "customer": customer, "quantity": quantity, "due": due,
                                       "price_each": price, "notes": "white"})


def test_taking_an_order_plans_its_run_and_follows_it(tmp_path):
    app = farm(tmp_path, "a", "b")
    c = TestClient(create_app(app))
    due = (date.today() + timedelta(days=30)).isoformat()
    r = take(c, due=due, price=2.5)
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["customer"] == "Sam's shop" and o["made"] == 0 and o["on_time"] is True and o["eta"] > time.time()
    assert o["money"]["revenue"] == 100.0 and "cost" in o["money"] and "profit" in o["money"]
    run = app.queue.get(o["run"])
    assert {j["printer"] for j in run["jobs"]} == {"a", "b"} and all(j["status"] == "waiting" for j in run["jobs"])

    first, second = run["jobs"][0], run["jobs"][1]
    app.queue.mark(run["id"], first["id"], status="done", loop=first["loops"])
    app.queue.mark(run["id"], second["id"], status="printing", loop=3)
    o = progress(app, app.orders.get(o["id"]))
    ppl = o["parts_per_loop"]
    assert o["made"] == min(40, (first["loops"] + 2) * ppl) and 0 < o["pct"] < 100 and not o["complete"]

    app.queue.mark(run["id"], second["id"], status="done")
    app.tell_queue()
    done = [e for e in app.store.events() if e["kind"] == "order_done"]
    assert len(done) == 1 and "Sam's shop" in done[0]["message"]
    assert app.orders.get(o["id"])["status"] == "done"
    app.tell_queue()
    assert len([e for e in app.store.events() if e["kind"] == "order_done"]) == 1  # told once
    app.shutdown()


def test_an_order_that_cant_be_ready_in_time_says_so(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    o = take(c, quantity=400, due=date.today().isoformat()).json()  # 200 loops on one printer, due tonight
    assert o["on_time"] is False and o["late_by_days"] >= 1
    s = summary(app)
    assert any(a["what"] == "order_late" for a in s["attention"]) and "Orders: Sam's shop 0/400" in s["text"]
    app.shutdown()


def test_changing_cancelling_and_removing(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    o = take(c).json()
    assert o["due"] is None and o["on_time"] is None  # no due date: no verdict
    r = c.patch(f"/api/orders/{o['id']}", json={"due": "2031-01-02", "status": "cancelled"}).json()
    assert r["due"] == "2031-01-02" and r["status"] == "cancelled"
    assert c.patch(f"/api/orders/{o['id']}", json={"due": "next week"}).status_code == 422
    assert c.patch(f"/api/orders/{o['id']}", json={"status": "lost"}).status_code == 422
    assert [x["id"] for x in c.get("/api/orders").json()] == [o["id"]]
    assert c.delete(f"/api/orders/{o['id']}").json() == {"removed": o["id"]}
    assert c.get("/api/orders").json() == [] and app.queue.runs() == []  # its queue went with it
    assert c.delete(f"/api/orders/{o['id']}").status_code == 404
    assert take(c, due="31/12/2030").status_code == 422 and app.queue.runs() == []  # nothing queued for it
    assert due_ts("2030-12-31") > due_ts("2030-12-30")
    app.shutdown()
