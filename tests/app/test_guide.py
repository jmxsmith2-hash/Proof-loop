"""The first-run guide ticks each step off from what the running ProofLoop knows."""

import pytest
from fastapi.testclient import TestClient

from proofloop.app import autostart
from proofloop.app.shell import HEADER, Shell
from proofloop.core.settings import Notifier, PrinterConfig

ORIGIN = "http://127.0.0.1:8600"
WINDOW = {HEADER: "1", "Origin": ORIGIN}


class FakeLink:
    def __init__(self, p):
        from proofloop.monitor.printer.state import PrinterState

        self.state = PrinterState(online=p.name != "offline")

    def add_listener(self, fn):
        pass

    def connect(self):
        pass


@pytest.fixture()
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(autostart, "enabled", lambda: False)
    s = Shell(8600, connect=False, link_factory=FakeLink)
    yield s
    s.close()


def done(c):
    return {s["id"]: s["done"] for s in c.get("/proofloop/guide", headers=WINDOW).json()["steps"]}


def test_a_new_setup_has_everything_to_do(shell):
    c = TestClient(shell.api, base_url=ORIGIN)
    assert c.get("/proofloop/guide").status_code == 403  # the window's own
    g = c.get("/proofloop/guide", headers=WINDOW).json()
    assert g["done"] == 0 and g["total"] == 7 and not g["complete"]
    steps = {s["id"]: s for s in g["steps"]}
    assert steps["printer"]["go"] == {"tab": "printers", "section": "monitor", "anchor": "addPrinter"}
    assert steps["autostart"]["go"] == {"call": "autostart"} and steps["slicer"]["optional"]


def test_steps_tick_off_as_you_go(shell, monkeypatch):
    c = TestClient(shell.api, base_url=ORIGIN)
    mon = shell.monitor
    mon.add_printer(PrinterConfig(name="p2s", host="127.0.0.1", serial="01", access_code="12345678", model_id="N7"))
    now = done(c)
    assert now["printer"] and now["connected"] and now["profile"]  # the P2S profile is built in
    assert not now["pictures"] and not now["floor"]
    pictures = mon.data_dir / "references" / "p2s"
    pictures.mkdir(parents=True)
    (pictures / "empty-1.jpg").write_bytes(b"\xff\xd8")
    mon.settings.notifications = [Notifier(kind="ntfy", url="https://ntfy.sh/x")]
    mon.update_defaults({"floor_calibrated": True})
    monkeypatch.setattr(autostart, "enabled", lambda: True)
    g = c.get("/proofloop/guide", headers=WINDOW).json()
    assert g["complete"] and g["done"] == g["total"]


def test_a_printer_that_isnt_connected_is_not_done(shell):
    c = TestClient(shell.api, base_url=ORIGIN)
    shell.monitor.add_printer(PrinterConfig(name="offline", host="127.0.0.1", serial="02", access_code="12345678",
                                            model_id="N7"))
    assert done(c)["printer"] and not done(c)["connected"]
