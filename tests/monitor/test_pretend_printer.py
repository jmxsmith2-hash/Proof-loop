"""End to end against a pretend printer that speaks the Bambu LAN protocol (``tests/fake_printer.py``).

The real path, over real sockets and TLS: ProofLoop connects over MQTT, reads the AMS, copies a
queued loop file over FTPS, starts it with ``project_file``, checks the plate with the camera at
each loop end, resumes only an empty plate, and ticks the job off when the printer finishes.
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import ImageDraw

pytest.importorskip("cryptography")

from proofloop.app.loop_server import create_app  # noqa: E402
from proofloop.core.settings import PrinterConfig, VerifyConfig  # noqa: E402
from proofloop.loopfiles import threemf  # noqa: E402
from proofloop.monitor.printer.bambu import BambuPrinter  # noqa: E402
from proofloop.monitor.send import SendError, upload  # noqa: E402
from proofloop.monitor.service import App  # noqa: E402
from tests.fake_printer import FakePrinter  # noqa: E402
from tests.monitor.test_vision import jpeg, plate  # noqa: E402

PLATE = Path(__file__).resolve().parents[2] / "examples" / "gcode" / "bracket_plate.gcode.3mf"


def picture(parts_left: bool) -> bytes:
    """The camera's view of the build plate, empty or with a part left on it."""
    img = plate(1)
    if parts_left:
        ImageDraw.Draw(img).rectangle((300, 250, 340, 290), fill=(230, 230, 230))
    return jpeg(img)


@pytest.fixture
def printer():
    p = FakePrinter()
    yield p
    p.close()


def connect(tmp_path, printer):
    def link(p):
        b = BambuPrinter(p.host, p.serial, p.access_code, p.model_id, p.snapshot_url)
        b.mqtt_port, b.ftp_port = printer.mqtt_port, printer.ftp_port
        return b

    app = App(tmp_path, connect=True, link_factory=link)
    cfg = PrinterConfig(name="p2s", host="127.0.0.1", serial=printer.serial, access_code=printer.access_code,
                        model_id="N7", snapshot_url=printer.snapshot_url, verify=VerifyConfig(settle_s=0))
    app.add_printer(cfg)
    app.verifier(cfg).add_reference(picture(parts_left=False))
    state = app.watches["p2s"].link.state
    printer.wait_for(lambda: state.online and state.ams.trays())
    return app


def test_a_queued_job_is_sent_printed_checked_resumed_and_ticked_off(tmp_path, printer):
    app = connect(tmp_path, printer)
    w = app.watches["p2s"]
    try:
        app.settings.send_to_printer = True
        c = TestClient(create_app(app))
        token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
        run = c.post("/api/queue", json={"token": token, "parts": 4, "settings": {"wait_for_monitor": True}}).json()
        job = run["jobs"][0]
        assert job["printer"] == "p2s" and job["loops"] >= 2

        r = c.post(f"/api/queue/{run['id']}/{job['id']}/send", json={"confirm": "p2s"})
        assert r.status_code == 200, r.text
        name = c.get("/api/queue").json()[0]["jobs"][0]["file"]
        assert printer.logins == [("bblp", printer.access_code)]
        sent = threemf.loads(printer.files[name], name=name)  # what arrived over FTPS is the whole loop file
        assert sent.md5_ok and name.startswith("FL_S2_")
        start = printer.command("project_file")
        assert start["file"] == name and start["url"] == f"file:///sdcard/{name}"
        assert start["use_ams"] and start["ams_mapping"] == [0]

        printer.wait_for(lambda: w.link.state.gcode_state == "RUNNING")
        app.tell_queue()
        assert c.get("/api/queue").json()[0]["jobs"][0]["status"] == "printing"

        # Loop 1 ends with an empty plate: the monitor checks it and resumes.
        printer.picture = picture(parts_left=False)
        printer.report(gcode_state="PAUSE", layer_num=999, total_layer_num=999)
        printer.wait_for(lambda: printer.command("resume"))
        printer.wait_for(lambda: w.link.state.gcode_state == "RUNNING")

        # Loop 2 ends with parts left behind: the printer keeps waiting and you're told.
        printer.report(layer_num=1)
        printer.picture = picture(parts_left=True)
        printer.report(gcode_state="PAUSE", layer_num=999)
        printer.wait_for(lambda: w.waiting_for_you, timeout=15)
        time.sleep(0.3)
        assert [x["command"] for x in printer.commands].count("resume") == 1
        assert w.link.state.gcode_state == "PAUSE"

        # You clear the plate and resume on the printer; the file finishes.
        printer.report(gcode_state="RUNNING", layer_num=1)
        printer.report(gcode_state="FINISH", layer_num=999)
        printer.wait_for(lambda: w.link.state.gcode_state == "FINISH")
        app.tell_queue()
        assert c.get("/api/queue").json()[0]["jobs"][0]["status"] == "done"
        assert any(e["kind"] == "queue_done" for e in app.store.events())
    finally:
        app.shutdown()


def test_a_wrong_access_code_is_refused_by_the_printer(printer):
    with pytest.raises(SendError, match="couldn't copy"):
        upload("127.0.0.1", "wrong", "x.gcode.3mf", b"x", timeout=5, port=printer.ftp_port)
    assert not printer.files
    upload("127.0.0.1", printer.access_code, "x.gcode.3mf", b"x" * 300_000, timeout=5, port=printer.ftp_port)
    assert printer.files["x.gcode.3mf"] == b"x" * 300_000
