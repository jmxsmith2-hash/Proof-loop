import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from synthux.app.server import file_kind, safe_name, serve
from synthux.core.config import load_settings
from synthux.core.db import Database

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture()
def app(tmp_path):
    cfg = tmp_path / "config"
    (cfg / "calibration").mkdir(parents=True)
    for name in ("settings.example.yaml", "models.yaml", "scales.yaml", "printers.yaml"):
        (cfg / name).write_text((REPO / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (cfg / "calibration" / "default.yaml").write_text(
        (REPO / "config" / "calibration" / "default.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    settings = load_settings(cfg).model_copy(update={"data_dir": tmp_path / "data"})
    db = Database(settings.db_path)
    server, the_app = serve(settings, db, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield the_app
    server.shutdown()
    db.close()


def _req(app, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=30)
    conn.request(method, path, body=body, headers={"Host": f"127.0.0.1:{app.port}", **(headers or {})})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, data


def _wait(app, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        _, data = _req(app, "GET", "/api/jobs")
        job = next(j for j in json.loads(data) if j["id"] == job_id)
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_page_and_gcode_job_with_report(app):
    status, page = _req(app, "GET", "/")
    assert status == 200 and b"Drop a G-code file here" in page and b"Bambu Lab P2S" in page
    body = (REPO / "examples" / "gcode" / "bracket_flawed.gcode").read_bytes()
    status, data = _req(app, "POST", "/api/check?name=bracket_flawed.gcode", body, {"X-Synthux": "1"})
    assert status == 201
    job = _wait(app, json.loads(data)["id"])
    assert job["status"] == "done" and job["counts"]["fail"] == 1
    assert job["summary"][0] == "will fail: Prints outside the build plate"
    status, html = _req(app, "GET", job["report_url"])
    assert status == 200 and b"bracket_flawed.gcode" in html
    status, png = _req(app, "GET", job["report_url"] + "plate.png")
    assert status == 200 and png[:4] == b"\x89PNG"
    assert _req(app, "GET", job["report_url"] + "../../../config/settings.example.yaml")[0] in (403, 404)


def test_other_pages_and_hosts_cannot_drive_it(app):
    body = b"G1 X1 E1\n"
    assert _req(app, "POST", "/api/check?name=a.gcode", body)[0] == 403                       # no app header
    assert _req(app, "POST", "/api/check?name=a.gcode", body,
                {"X-Synthux": "1", "Origin": "https://evil.example"})[0] == 403
    assert _req(app, "GET", "/api/jobs", headers={"Host": f"evil.example:{app.port}"})[0] == 403  # DNS rebinding
    status, data = _req(app, "POST", "/api/check?name=notes.txt", b"hello", {"X-Synthux": "1"})
    assert status == 400 and "drop a CAD file" in json.loads(data)["error"]


def test_website_off_the_allowlist_needs_the_users_tick(app):
    form = {"url": "https://shop.example.com/", "tasks": ["Buy a lamp"], "successes": ["Thank you"]}
    status, data = _req(app, "POST", "/api/test", json.dumps(form), {"X-Synthux": "1"})
    assert status == 400 and json.loads(data)["needs_allow"]
    assert not (app.settings.config_dir / "settings.yaml").exists()                         # nothing changed
    status, data = _req(app, "POST", "/api/test", json.dumps({**form, "successes": []}), {"X-Synthux": "1"})
    assert status == 400


def test_upload_names_are_made_safe():
    assert safe_name("..\\..\\evil/../x.stl") == "x.stl"
    assert safe_name("bad<>:name.gcode") == "bad_name.gcode"
    assert file_kind("Part.GCODE.3MF") == "gcode" and file_kind("part.3mf") == "cad" and file_kind("a.pdf") is None


def test_make_a_loop_file_from_a_checked_plate(app):
    pytest.importorskip("clearloop")
    body = (REPO / "examples" / "gcode" / "bracket_plate.gcode.3mf").read_bytes()
    status, data = _req(app, "POST", "/api/check?name=bracket_plate.gcode.3mf", body, {"X-Synthux": "1"})
    plate = _wait(app, json.loads(data)["id"])
    assert plate["status"] == "done" and plate["can_loop"]
    status, data = _req(app, "POST", "/api/loop", json.dumps({"job": plate["id"], "loops": 3}).encode(),
                        {"X-Synthux": "1"})
    assert status == 201, data
    job = _wait(app, json.loads(data)["id"])
    assert job["status"] == "done", job["error"]
    assert any("loop file by ClearLoop: 3 loops" in s for s in job["summary"]) and not job["can_loop"]
    status, made = _req(app, "GET", job["download"])
    assert status == 200 and made[:2] == b"PK"


def test_loop_needs_a_plate_and_the_app_window(app):
    status, data = _req(app, "POST", "/api/loop", b'{"job": "nope", "loops": 3}', {"X-Synthux": "1"})
    assert status == 400 and b"no Bambu plate" in data
    assert _req(app, "POST", "/api/loop", b'{"job": "nope", "loops": 3}')[0] == 403


def test_long_jobs_send_a_notification(app, monkeypatch):
    from synthux import notify
    from synthux.app import server as srv

    sent = []
    monkeypatch.setattr(notify, "send", lambda title, msg, link=None: sent.append((title, msg)) or 1)
    monkeypatch.setattr(srv, "NOTIFY_AFTER_S", 0)
    body = (REPO / "examples" / "gcode" / "bracket_good.gcode").read_bytes()
    _, data = _req(app, "POST", "/api/check?name=bracket_good.gcode", body, {"X-Synthux": "1"})
    _wait(app, json.loads(data)["id"])
    time.sleep(0.2)
    assert sent and sent[0][0] == "Synthux: bracket_good.gcode is done"


def test_only_the_proofloop_window_may_frame_it(app, tmp_path):
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=30)
    conn.request("GET", "/", headers={"Host": f"127.0.0.1:{app.port}"})
    res = conn.getresponse()
    page = res.read()
    assert res.getheader("X-Frame-Options") == "DENY" and b"Fine-tune in ClearLoop" in page  # the JS, hidden alone
    assert b"const SHELL = null" in page

    app.shell = "http://127.0.0.1:8600"
    conn.request("GET", "/", headers={"Host": f"127.0.0.1:{app.port}"})
    res = conn.getresponse()
    page = res.read()
    conn.close()
    assert res.getheader("X-Frame-Options") is None
    assert res.getheader("Content-Security-Policy") == "frame-ancestors http://127.0.0.1:8600"
    assert b'const SHELL = "http://127.0.0.1:8600"' in page and b"<h1>Synthux</h1>" not in page
    with pytest.raises(ValueError):
        serve(app.settings, app.db, port=0, shell="https://example.com")


def test_plates_handed_over_by_proofloop(app):
    pytest.importorskip("clearloop")
    plate = REPO / "examples" / "gcode" / "bracket_plate.gcode.3mf"
    job = app.submit_bytes(plate.name, plate.read_bytes())
    assert _wait(app, job.id)["can_loop"]
    assert app.job_source(job.id) == (plate.read_bytes(), plate.name)
    with pytest.raises(ValueError):
        app.submit_bytes("empty.gcode.3mf", b"")
    flat = app.submit_bytes("bracket_flawed.gcode", (REPO / "examples" / "gcode" / "bracket_flawed.gcode").read_bytes())
    _wait(app, flat.id)
    with pytest.raises(ValueError):
        app.job_source(flat.id)  # plain G-code: nothing for ClearLoop to loop


def test_jobs_are_kept_after_the_app_closes(app):
    body = (REPO / "examples" / "gcode" / "bracket_flawed.gcode").read_bytes()
    _, data = _req(app, "POST", "/api/check?name=bracket_flawed.gcode", body, {"X-Synthux": "1"})
    job = _wait(app, json.loads(data)["id"])
    from synthux.app.server import App

    again = App(app.settings, app.db, 0)  # the app opened again
    assert job["id"] in again.jobs and again.jobs[job["id"]].status == "done"
    assert again.jobs[job["id"]].findings and again.jobs[job["id"]].printer
    again.close()
    status, _ = _req(app, "POST", "/api/remove", json.dumps({"job": job["id"]}).encode(), {"X-Synthux": "1"})
    assert status == 200 and job["id"] not in app.jobs
    third = App(app.settings, app.db, 0)
    assert job["id"] not in third.jobs
    third.close()


def test_false_alarms_count_lower_next_time(app):
    body = (REPO / "examples" / "gcode" / "bracket_flawed.gcode").read_bytes()
    _, data = _req(app, "POST", "/api/check?name=bracket_flawed.gcode", body, {"X-Synthux": "1"})
    job = _wait(app, json.loads(data)["id"])
    top = next(f for f in job["findings"] if f["severity"] >= 2)
    for _ in range(2):
        status, counts = _req(app, "POST", "/api/feedback",
                              json.dumps({"job": job["id"], "check": top["check"], "verdict": "wrong"}).encode(),
                              {"X-Synthux": "1"})
    assert status == 200 and json.loads(counts) == {"wrong": 2, "right": 0}
    _, data = _req(app, "POST", "/api/check?name=bracket_flawed.gcode", body, {"X-Synthux": "1"})
    again = _wait(app, json.loads(data)["id"])
    now = next(f for f in again["findings"] if f["check"] == top["check"])
    assert now["severity"] == top["severity"] - 1
    status, _ = _req(app, "POST", "/api/feedback",
                     json.dumps({"job": job["id"], "check": "nope", "verdict": "wrong"}).encode(), {"X-Synthux": "1"})
    assert status == 400


def test_print_and_website_views(app):
    _, page = _req(app, "GET", "/?view=print")
    assert b'id="g-drop"' in page and b'id="w-form"' not in page
    _, page = _req(app, "GET", "/?view=web")
    assert b'id="w-form"' in page and b'id="g-drop"' not in page and b'id="cad-drop"' not in page
    _, page = _req(app, "GET", "/")
    assert b'id="w-form"' in page and b'id="g-drop"' in page


def test_your_clearloop_printers_come_first(app, tmp_path, monkeypatch):
    pytest.importorskip("clearloop")
    from synthux.physical import learn

    cl = tmp_path / "clearloop"
    cl.mkdir()
    (cl / "settings.json").write_text(json.dumps({"printers": [{"name": "Farm 1", "model_id": "N7"},
                                                                {"name": "Big one", "model_id": "O1D"}]}),
                                      encoding="utf-8")
    monkeypatch.setattr(learn, "CLEARLOOP_DIR", cl)
    _, page = _req(app, "GET", "/")
    assert b'<optgroup label="Your printers">' in page
    assert b"Farm 1 (Bambu Lab P2S)" in page and b"Big one (Bambu Lab H2D)" in page
    from synthux.physical.my_printers import default_printer

    assert default_printer({"printers": {"bambu_h2d": {"names": ["Bambu Lab H2D"]}, "bambu_p2s":
                                         {"names": ["Bambu Lab P2S"]}}, "default_printer": "x"}, cl) == "bambu_p2s"
