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
