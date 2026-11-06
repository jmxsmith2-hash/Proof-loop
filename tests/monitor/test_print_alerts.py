"""Alerts when any print finishes or fails: the events, the setting that turns them off, who is told,
and the printers' overview being the screen ProofLoop opens on."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core import notify
from proofloop.core.settings import Notifier, PrintAlerts, Settings
from proofloop.monitor.printer.state import PrinterState
from proofloop.monitor.service import App
from proofloop.monitor.store import Store
from proofloop.monitor.watch import PrinterWatch

STATIC = Path(__file__).resolve().parents[2] / "src" / "proofloop" / "app" / "static"


class Link:
    def __init__(self):
        self.state = PrinterState(online=True, gcode_state="RUNNING", subtask_name="benchy", layer_num=40, total_layers=100)

    def snapshot(self):
        return b"jpeg"


class Verifier:
    references: list = []


def make():
    events = []
    link = Link()
    w = PrinterWatch("p2s", link, Verifier(), Store(), lambda e, image=None: events.append(e),
                     settle_s=0, run_async=lambda f: None, retry_s=0)
    w.on_state(link.state)
    return w, link, events


def end(w, link, state):
    link.state.gcode_state = state
    w.on_state(link.state)


def test_a_plain_print_that_finishes_raises_an_event():
    w, link, events = make()  # "benchy" is not a loop file
    end(w, link, "FINISH")
    done = [e for e in events if e["kind"] == "print_done"]
    assert len(done) == 1 and "benchy" in done[0]["message"] and done[0]["printer"] == "p2s"


def test_a_failed_print_names_the_job():
    w, link, events = make()
    end(w, link, "FAILED")
    failed = [e for e in events if e["kind"] == "failed"]
    assert len(failed) == 1 and failed[0]["level"] == "error" and "benchy" in failed[0]["message"]


def test_alerts_are_on_by_default():
    assert Settings().print_alerts == PrintAlerts(finished=True, failed=True)


def test_print_alert_says_which_events_the_setting_covers():
    on, off = PrintAlerts(), PrintAlerts(finished=False, failed=False)
    assert notify.print_alert("print_done", on) is True and notify.print_alert("failed", on) is True
    assert notify.print_alert("print_done", off) is False and notify.print_alert("failed", off) is False
    only_done = PrintAlerts(failed=False, finished=True)
    assert notify.print_alert("failed", only_done) is False and notify.print_alert("print_done", only_done) is True
    assert notify.print_alert("hms", off) is None  # other events are not this setting's business


class Sink:
    def __init__(self):
        self.sent = []

    def send(self, event, image=None, wait=False, every=False):
        self.sent.append((event["kind"], every))


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path, connect=False)
    a.notifier = Sink()
    return a


def ev(kind, level="info"):
    return {"kind": kind, "level": level, "message": "x", "printer": "p2s"}


def test_finish_and_fail_go_to_every_target_when_on(app):
    app._emit(ev("print_done"))
    app._emit(ev("failed", "error"))
    app._emit(ev("loop"))
    assert app.notifier.sent == [("print_done", True), ("failed", True), ("loop", False)]


def test_switching_them_off_silences_only_those(app):
    app.settings.print_alerts = PrintAlerts(finished=False, failed=True)
    app._emit(ev("print_done"))
    app._emit(ev("failed", "error"))
    app._emit(ev("hms", "error"))
    assert app.notifier.sent == [("failed", True), ("hms", False)]
    app.settings.print_alerts = PrintAlerts(finished=False, failed=False)
    app._emit(ev("failed", "error"))
    assert len(app.notifier.sent) == 2


def test_every_reaches_a_target_whose_lowest_level_is_error():
    sent = []
    n = notify.Notifier([Notifier(kind="webhook", url="http://x.invalid/", min_level="error")])
    n._deliver = lambda cfg, event, image: sent.append(event["kind"]) or True
    n.send(ev("print_done"), wait=True)
    assert sent == []  # a plain info event is held back by the target's level
    n.send(ev("print_done"), every=True, wait=True)
    assert sent == ["print_done"]


def test_the_setting_is_saved_shown_and_window_only(tmp_path):
    a = App(tmp_path, connect=False)
    c, phone = TestClient(create_app(a)), TestClient(create_app(a, allow_send=False))
    assert c.get("/api/state").json()["print_alerts"] == {"finished": True, "failed": True}
    assert c.put("/api/print-alerts", json={"finished": False, "failed": True}).json() == {"finished": False, "failed": True}
    assert a.settings.print_alerts.finished is False
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["print_alerts"]["finished"] is False
    assert phone.put("/api/print-alerts", json={"finished": True, "failed": True}).status_code == 403
    assert c.put("/api/print-alerts", json={"finished": "maybe"}).status_code == 422
    assert 'id="alertForm"' in c.get("/").text


def test_window_and_page_open_on_the_printers_overview():
    shell = (STATIC / "shell.html").read_text(encoding="utf-8")
    assert 'const DEFAULT_TAB = "printers"' in shell and "show(DEFAULT_TAB)" in shell
    assert 'show(FRAME[saved] ? saved : "check")' not in shell
    core = (STATIC / "js" / "core.js").read_text(encoding="utf-8")
    main = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
    assert "const DEFAULT_TAB = 'overview'" in core
    assert "DEFAULT_TAB" in main and "'make'" not in main
    first = re.search(r"printers: \[([^\]]*)\]", core).group(1).split(",")[0]
    assert first.strip() == "'overview'"  # the Printers part starts on its overview
    assert 'data-panel="overview"' in (STATIC / "loop.html").read_text(encoding="utf-8")


def test_the_overview_shows_picture_progress_time_job_and_ams_per_printer():
    js = (STATIC / "js" / "overview.js").read_text(encoding="utf-8")
    glance = js[js.index("function printerGlance"):js.index("async function loadSnaps")]
    for part in ("/api/monitor/", "p.percent", "remaining_min", "p.job", "amsStrip(p)"):
        assert part in js
    assert "printerGlance(r.printer)" in js
    assert "snapshot" in js and "data-ovsnap" in js and "percent" in glance
