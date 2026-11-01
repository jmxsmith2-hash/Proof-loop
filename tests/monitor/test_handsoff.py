"""The hands-off queue: a printer's next queued job is sent by itself once its plate checks empty."""

import time

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.monitor.handsoff import ASK_EVERY_S, HOLD_S
from proofloop.monitor.vision import BedCheck
from tests.monitor.test_queue import farm, jobs, queued


def hands_off(tmp_path, *printers, confirm=False):
    app = farm(tmp_path, *printers)
    create_app(app)  # the window's API: it tells the hands-off queue how to make a job's file
    app.settings.send_to_printer, app.settings.send_confirm = True, confirm
    for p in app.settings.printers:
        p.hands_off = True
    return app


def plate(app, printer, clear=True, at=None):
    w = app.watches[printer]
    w.last_check = BedCheck(clear, 0.0 if clear else 0.2, 0, "ref.png")
    w.last_check_ts = time.time() if at is None else at


def test_nothing_is_sent_without_hands_off_or_sending_on(tmp_path):
    app = hands_off(tmp_path, "a")
    queued(app, "a")
    plate(app, "a")
    app.settings.printers[0].hands_off = False
    assert app.hands_off.tick() == []
    app.settings.printers[0].hands_off, app.settings.send_to_printer = True, False
    assert app.hands_off.tick() == [] and not app.watches["a"].link.sent
    app.shutdown()


def test_it_looks_at_the_plate_first_and_sends_only_when_it_is_empty(tmp_path):
    app = hands_off(tmp_path, "a")
    asked = []
    app.watches["a"].check_again = lambda: asked.append(1)
    queued(app, "a", "a")
    now = time.time()
    assert app.hands_off.tick(now) == ["a: checking the plate"] and asked == [1]
    assert app.hands_off.tick(now + 10) == []  # asked once, not every tick
    plate(app, "a", clear=False)
    assert app.hands_off.tick(now + ASK_EVERY_S + 1) == []  # parts left: it waits for you
    plate(app, "a")
    assert app.hands_off.tick() == ["a: sent"]
    assert app.watches["a"].link.sent and jobs(app)[0][0] == "sent" and jobs(app)[1][0] == "waiting"
    assert any(e["kind"] == "queue_auto_sent" for e in app.store.events())
    assert app.hands_off.tick() == []  # the next job waits until this one has run
    app.shutdown()


def test_a_check_from_before_the_last_print_is_not_trusted(tmp_path):
    app = hands_off(tmp_path, "a")
    app.watches["a"].check_again = lambda: None
    queued(app, "a")
    plate(app, "a", at=time.time() - 60)
    app.watches["a"].ended_at = time.time() - 30  # the last print ended after that check
    assert app.hands_off.tick() == ["a: checking the plate"] and not app.watches["a"].link.sent
    app.shutdown()


def test_with_ask_before_each_send_you_are_told_first_and_can_hold_it(tmp_path):
    app = hands_off(tmp_path, "a", confirm=True)
    rid, (jid,) = queued(app, "a")
    plate(app, "a")
    now = time.time()
    assert app.hands_off.tick(now) == ["a: sending soon"]
    assert "in 2 minutes" in next(e for e in app.store.events() if e["kind"] == "queue_auto_soon")["message"]
    assert app.hands_off.tick(now + HOLD_S / 2) == [] and not app.watches["a"].link.sent
    c = TestClient(create_app(app))
    assert c.post(f"/api/queue/{rid}/{jid}/hold", json={"held": True}).json()["held"] is True
    assert app.hands_off.tick(now + HOLD_S + 1) == []  # held: left for you
    c.post(f"/api/queue/{rid}/{jid}/hold", json={"held": False})
    plate(app, "a")
    assert app.hands_off.tick(now + HOLD_S + 2) == ["a: sending soon"]  # a fresh notice after a hold
    assert app.hands_off.tick(now + 2 * HOLD_S + 3) == ["a: sent"]
    app.shutdown()


def test_a_failed_send_holds_the_job_and_tells_you(tmp_path):
    app = hands_off(tmp_path, "a")
    queued(app, "a")
    plate(app, "a")

    def broken(rid, jid):
        raise OSError("the printer refused the upload")

    app.hands_off.make = broken
    assert app.hands_off.tick() == ["a: failed"]
    assert app.queue.runs()[0]["jobs"][0]["held"] is True
    assert "refused the upload" in next(e for e in app.store.events() if e["kind"] == "queue_auto_failed")["message"]
    assert app.hands_off.tick() == []  # never retried by itself
    app.shutdown()


def test_a_stopped_job_waits_for_you(tmp_path):
    app = hands_off(tmp_path, "a")
    rid, (first, _) = queued(app, "a", "a")
    app.queue.mark(rid, first, status="stopped", why="failed in loop 2")
    plate(app, "a")
    assert app.hands_off.tick() == []
    app.shutdown()


def test_the_window_switches_it_and_the_free_printer_notice_is_left_to_it(tmp_path):
    app = hands_off(tmp_path, "a")
    app.settings.printers[0].hands_off = False
    c = TestClient(create_app(app))
    assert c.put("/api/monitor/a/hands-off", json={"on": True}).json()["hands_off"] is True
    assert c.get("/api/state").json()["monitor"][0]["hands_off"] is True
    phone = TestClient(create_app(app, allow_send=False))
    assert phone.put("/api/monitor/a/hands-off", json={"on": False}).status_code == 403
    queued(app, "a")
    app.tell_queue()
    assert not [e for e in app.store.events() if e["kind"] == "queue_ready"]  # hands-off says what it does
    app.shutdown()
