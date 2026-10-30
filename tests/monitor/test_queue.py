"""Spreading a run over the printers that can print it, and the queue that never starts anything itself."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import PrinterConfig
from proofloop.loopfiles import threemf
from proofloop.monitor.queue import Queue, spread
from proofloop.monitor.service import App
from tests.monitor.test_send import WHITE_PLA, state

PLATE = Path(__file__).resolve().parents[2] / "examples" / "gcode" / "bracket_plate.gcode.3mf"
RED_PLA = {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF"}]}]}}


class FakeLink:
    def __init__(self, p):
        ams = RED_PLA if p.name == "red" else WHITE_PLA
        self.state = state("RUNNING" if p.name == "busy" else "IDLE", online=p.name != "offline", ams=ams)
        if p.name == "busy":
            self.state.remaining_min = 600
        self.sent = []

    def add_listener(self, fn):
        pass

    def connect(self):
        pass

    def send(self, filename, data, command):
        self.sent.append(filename)


def farm(tmp_path, *names, model="N7"):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    for i, n in enumerate(names):
        app.add_printer(PrinterConfig(name=n, host="127.0.0.1", serial=f"0{i}", access_code="12345678",
                                      model_id="O1D" if n == "h2d" else model))
    return app


def test_loops_go_where_they_finish_first(tmp_path):
    app = farm(tmp_path, "a", "b", "busy", "offline", "red", "h2d")
    for _ in range(3):
        app.store.add_timing("a", "cooldown", 600)   # a cools in 10 min,
        app.store.add_timing("b", "cooldown", 3000)  # b takes 50
    sf = threemf.load(PLATE)
    plan = spread(app, sf, "N7", 1200, 2, 40)  # 20 min of print and fixed steps per loop, 2 parts per loop
    assert plan["loops"] == 20 and plan["parts"] == 40
    rows = {r["printer"]: r for r in plan["rows"]}
    assert rows["a"]["loops"] > rows["b"]["loops"] > 0
    assert sum(r["loops"] for r in plan["rows"]) == 20
    assert rows.get("busy", {"loops": 0})["loops"] < rows["b"]["loops"]  # free in 10 h: little or nothing
    why = {x["printer"]: x["why"] for x in plan["left_out"]}
    assert why["offline"] == "not connected" and why["h2d"] == "another printer model"
    assert "no PLA #F2F2F2 loaded" in why["red"]
    assert abs(rows["a"]["finish_s"] - rows["b"]["finish_s"]) < 2 * 3000 + 1200  # they finish close together
    app.shutdown()


def test_a_long_run_on_one_printer_is_split_into_files(tmp_path):
    app = farm(tmp_path, "a")
    sf = threemf.load(PLATE)
    plan = spread(app, sf, "N7", 1200, 2, 240)
    assert plan["rows"][0]["loops"] == 120 and plan["rows"][0]["files"] == 3
    q = Queue(tmp_path / "queue")
    run = q.add(PLATE.name, PLATE.read_bytes(), {"floor_z": 2.5, "loops": 9}, plan)
    assert [j["loops"] for j in run["jobs"]] == [50, 50, 20] and "loops" not in run["settings"]
    assert q.plate(run["id"]) == (PLATE.read_bytes(), PLATE.name)
    with pytest.raises(ValueError, match="no printer can print"):
        q.add(PLATE.name, PLATE.read_bytes(), {}, {"rows": [], "left_out": [], "parts": 0})
    app.shutdown()


def test_you_are_told_once_when_a_printer_comes_free(tmp_path):
    app = farm(tmp_path, "a", "busy")
    sf = threemf.load(PLATE)
    plan = spread(app, sf, "N7", 1200, 2, 8)
    plan["rows"].append({"printer": "busy", "loops": 1, "parts": 2})
    app.queue.add(PLATE.name, PLATE.read_bytes(), {}, plan)
    app.tell_queue()
    app.tell_queue()
    told = [e for e in app.store.events() if e["kind"] == "queue_ready"]
    assert [e["printer"] for e in told] == ["a"] and "Next in the queue" in told[0]["message"]
    app.watches["busy"].link.state.gcode_state = "FINISH"
    app.tell_queue()
    assert sorted(e["printer"] for e in app.store.events() if e["kind"] == "queue_ready") == ["a", "busy"]
    app.shutdown()


def test_plan_queue_download_and_send_from_the_window(tmp_path):
    app = farm(tmp_path, "a", "b")
    c = TestClient(create_app(app))
    token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
    plan = c.post(f"/api/plates/{token}/spread", json={"parts": 10, "settings": {}}).json()
    assert {r["printer"] for r in plan["rows"]} == {"a", "b"}
    assert c.post(f"/api/plates/{token}/spread", json={"parts": 0}).status_code == 422
    run = c.post("/api/queue", json={"token": token, "parts": 10, "settings": {}}).json()
    first, second = run["jobs"][0], run["jobs"][1]
    r = c.post(f"/api/queue/{run['id']}/{first['id']}/download")
    assert r.status_code == 200 and r.content[:2] == b"PK" and r.headers["x-proofloop-filename"].startswith("FL_S2_")
    assert c.post(f"/api/queue/{run['id']}/{second['id']}/send", json={"confirm": second["printer"]}).status_code == 409
    app.settings.send_to_printer = True
    assert c.post(f"/api/queue/{run['id']}/{second['id']}/send", json={"confirm": "nope"}).status_code == 400
    r = c.post(f"/api/queue/{run['id']}/{second['id']}/send", json={"confirm": second["printer"]})
    assert r.status_code == 200, r.text
    assert app.watches[second["printer"]].link.sent
    jobs = {j["id"]: j["status"] for j in c.get("/api/queue").json()[0]["jobs"]}
    assert jobs == {first["id"]: "downloaded", second["id"]: "sent"}
    phone = TestClient(create_app(app, allow_send=False))
    assert phone.post(f"/api/queue/{run['id']}/{second['id']}/send", json={"confirm": second["printer"]}).status_code == 403
    assert c.delete(f"/api/queue/{run['id']}").json() == {"removed": run["id"]}
    assert c.get("/api/queue").json() == [] and c.delete(f"/api/queue/{run['id']}").status_code == 404
    app.shutdown()


def queued(app, *printers, loops=3):
    run = app.queue.add(PLATE.name, PLATE.read_bytes(), {}, {"parts": 6, "left_out": [],
                        "rows": [{"printer": p, "loops": loops} for p in printers]})
    return run["id"], [j["id"] for j in run["jobs"]]


def jobs(app):
    return [(j["status"], j.get("loop"), j.get("why")) for j in app.queue.runs()[0]["jobs"]]


def running(app, printer, name, gcode_state="RUNNING", loop=1, error=0):
    s = app.watches[printer].link.state
    s.gcode_state, s.subtask_name, s.print_error = gcode_state, name, error
    app.watches[printer].loop_no = loop


def test_a_sent_job_is_followed_to_done_and_only_then_is_the_next_one_offered(tmp_path):
    app = farm(tmp_path, "a")
    rid, (first, second) = queued(app, "a", "a")
    app.queue.mark(rid, first, status="sent", file="FL_S2_bracket.gcode.3mf", at=time.time())
    app.tell_queue()  # the printer hasn't started the file yet: the next job isn't offered
    assert jobs(app)[0][0] == "sent" and not [e for e in app.store.events() if e["kind"] == "queue_ready"]
    running(app, "a", "FL_S2_bracket", loop=2)
    app.tell_queue()
    assert jobs(app)[0] == ("printing", 2, None)
    running(app, "a", "FL_S2_bracket", "PAUSE", loop=3)  # a loop-end pause is still the same job
    app.tell_queue()
    assert jobs(app)[0] == ("printing", 3, None)
    running(app, "a", "FL_S2_bracket", "FINISH", loop=3)
    app.tell_queue()
    assert jobs(app) == [("done", 3, None), ("waiting", None, None)]
    kinds = [e["kind"] for e in app.store.events()]
    assert "queue_done" in kinds and "queue_ready" in kinds
    app.tell_queue()  # told once
    assert [e["kind"] for e in app.store.events()].count("queue_done") == 1
    app.shutdown()


def test_a_failed_or_cancelled_job_can_be_sent_again(tmp_path):
    app = farm(tmp_path, "a", "b")
    rid, (ja, jb) = queued(app, "a", "b")
    for p, j in (("a", ja), ("b", jb)):
        app.queue.mark(rid, j, status="downloaded", file="FL_S2_bracket.gcode.3mf", at=time.time())
        running(app, p, "FL_S2_bracket.gcode.3mf", loop=2)
    app.tell_queue()
    running(app, "a", "FL_S2_bracket", "FAILED", loop=2, error=0x07008011)
    running(app, "b", "FL_S2_bracket", "FAILED", loop=2, error=0x0300400C)
    app.tell_queue()
    assert jobs(app) == [("stopped", 2, "failed in loop 2"), ("stopped", 2, "cancelled")]
    stopped = [e for e in app.store.events() if e["kind"] == "queue_stopped"]
    assert len(stopped) == 2 and stopped[0]["level"] == "error"
    app.settings.send_to_printer = True
    for p in ("a", "b"):
        app.watches[p].link.state.gcode_state = "IDLE"
    r = TestClient(create_app(app)).post(f"/api/queue/{rid}/{ja}/send", json={"confirm": "a"})
    assert r.status_code == 200 and jobs(app)[0] == ("sent", None, None)
    app.shutdown()


def test_a_file_the_printer_never_starts_stops_the_job(tmp_path):
    app = farm(tmp_path, "a")
    rid, (j,) = queued(app, "a")
    app.queue.mark(rid, j, status="sent", file="FL_S2_bracket.gcode.3mf", at=time.time() - 3600)
    app.tell_queue()
    assert jobs(app) == [("stopped", None, "the printer didn't start it")]
    app.shutdown()


def test_a_print_that_ended_unseen_is_read_from_the_print_history(tmp_path):
    app = farm(tmp_path, "a", "b")
    rid, (ja, jb) = queued(app, "a", "b")
    for p, j in (("a", ja), ("b", jb)):
        app.queue.mark(rid, j, status="printing", file="FL_S2_bracket.gcode.3mf", at=time.time() - 60)
        app.store.add_outcome({"printer": p, "job": "FL_S2_bracket", "outcome": "finished" if p == "a" else "failed"})
        running(app, p, "something_else")
    app.tell_queue()
    assert [s for s, _, _ in jobs(app)] == ["done", "stopped"]
    app.shutdown()
