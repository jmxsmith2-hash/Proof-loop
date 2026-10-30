"""The summary of what happened on your printers, and the daily one sent at the time you chose."""

import time
from datetime import datetime

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.monitor.summary import Morning, summary
from tests.monitor.test_queue import farm


def busy_night(app):
    for loop in range(1, 7):
        app.store.add_production("a", "FL_S2_bracket", loop, 4, 20.0)
    app.store.add_production("b", "FL_S2_bracket", 1, 4, 20.0)
    app.store.add_production("b", "FL_S2_bracket", 2, 4, 9.0, ok=False)
    app.store.add_outcome({"printer": "b", "job": "FL_S2_bracket", "outcome": "failed", "loop_no": 2, "layer": 41,
                           "total_layers": 120, "hms": [{"code": "0300-0D00-0001-000B", "level": "error"}]})
    app.store.add_outcome({"printer": "a", "job": "FL_S2_bracket", "outcome": "finished", "eject": "parts_left"})
    app.store.add_event("a", "error", "plate_not_clear", "parts left on the plate (loop end). The printer is waiting.")
    app.store.add_event("a", "info", "loop", "cooling")
    app.store.add_usage(grams=120.0, kind="loop", printer="a", material="PLA", color="#F2F2F2")


def test_the_summary_counts_the_night_and_says_what_needs_you(tmp_path):
    app = farm(tmp_path, "a", "b")
    busy_night(app)
    app.watches["a"].waiting_for_you = True
    app.watches["a"].status = "parts left on the plate: clear it"
    s = summary(app)
    assert (s["parts"], s["loops"], s["failed_loops"]) == (28, 7, 1)
    assert s["headline"] == "28 parts in 7 loops in the last 12 hours; 2 things need you."
    assert [a["what"] for a in s["attention"]] == ["waiting", "plate_not_clear"]
    f = s["failures"][0]
    assert (f["printer"], f["layer"], f["hms"]) == ("b", 41, ["0300-0D00-0001-000B"])
    assert s["parts_left"][0]["printer"] == "a"
    assert s["filament"]["by_material"] == {"PLA": 120.0} and s["filament"]["unassigned_g"] == 120.0
    assert "b failed FL_S2_bracket at layer 41/120, HMS 0300-0D00-0001-000B" in s["text"]
    assert summary(app, since=time.time() + 1)["loops"] == 0
    app.shutdown()


def test_the_daily_summary_is_sent_once_a_day_at_your_time(tmp_path):
    m = Morning(tmp_path)
    morning = datetime(2026, 10, 30, 7, 30).timestamp()
    assert m.due(None, morning) is None and m.due("07:30", morning - 60) is None
    assert m.due("07:30", morning + 60) == morning + 60 - 24 * 3600
    m.sent(morning + 60)
    assert m.due("07:30", morning + 3600) is None
    assert m.due("07:30", morning + 24 * 3600) == morning + 60  # tomorrow: since the last one

    app = farm(tmp_path / "farm", "a")
    busy_night(app)
    sent = []
    app.notifier.send = lambda event, image=None, wait=False, every=False: sent.append((event, every))
    app.settings.summary_at = "00:00"
    assert app.morning_summary()["loops"] == 6
    assert app.morning_summary() is None  # once a day
    (event, every), = sent
    assert every and event["kind"] == "summary" and event["message"].startswith("24 parts in 6 loops")
    app.shutdown()


def test_the_window_shows_it_and_sets_the_time(tmp_path):
    app = farm(tmp_path, "a")
    busy_night(app)
    c = TestClient(create_app(app))
    s = c.get("/api/summary?hours=24").json()
    assert s["loops"] == 6 and s["hours"] == 24 and s["at"] is None
    assert c.put("/api/summary-at", json={"at": "07:30"}).json() == {"at": "07:30"}
    assert app.settings.summary_at == "07:30" and c.get("/api/summary").json()["at"] == "07:30"
    assert c.put("/api/summary-at", json={"at": "25:00"}).status_code == 422
    assert c.put("/api/summary-at", json={"at": None}).json() == {"at": None}
    app.shutdown()
