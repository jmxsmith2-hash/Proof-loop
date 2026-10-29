"""The Check tab: its page and API on the window's own server, under /check."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proofloop.app.checks import Checks, file_kind, safe_name
from proofloop.app.shell import HEADER, Shell

REPO = Path(__file__).resolve().parents[2]
EX = REPO / "examples" / "gcode"
ORIGIN = "http://127.0.0.1:8600"
WINDOW = {HEADER: "1", "Origin": ORIGIN}


@pytest.fixture()
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path / "home"))
    (tmp_path / "config").mkdir()  # empty: the built-in config files are used
    s = Shell(8600, config=tmp_path / "config", connect=False)
    yield s
    s.close()


@pytest.fixture()
def c(shell):
    return TestClient(shell.api, base_url=ORIGIN)


def upload(c, name, body, **opts):
    q = "&".join(f"{k}={v}" for k, v in {"name": name, **opts}.items())
    return c.post(f"/check/api/check?{q}", content=body, headers=WINDOW)


def wait(c, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = next(j for j in c.get("/check/api/jobs").json() if j["id"] == job_id)
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_page_and_gcode_job_with_report(c):
    page = c.get("/check")
    assert page.status_code == 200 and "Drop a G-code file here" in page.text and "Bambu Lab P2S" in page.text
    assert page.headers["x-frame-options"] == "SAMEORIGIN"  # in the window's frame, never in another site
    r = upload(c, "bracket_flawed.gcode", (EX / "bracket_flawed.gcode").read_bytes())
    assert r.status_code == 201
    job = wait(c, r.json()["id"])
    assert job["status"] == "done" and job["counts"]["fail"] == 1
    assert job["summary"][0] == "will fail: Prints outside the build plate"
    assert job["report_url"].startswith("/check/job/")
    html = c.get(job["report_url"])
    assert html.status_code == 200 and "bracket_flawed.gcode" in html.text
    png = c.get(job["report_url"] + "plate.png")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert c.get(job["report_url"] + "..%2F..%2F..%2Fsettings.json").status_code in (403, 404)


def test_other_pages_and_hosts_cannot_drive_it(c, shell):
    body = b"G1 X1 E1\n"
    assert c.post("/check/api/check?name=a.gcode", content=body).status_code == 403          # no window header
    assert c.post("/check/api/check?name=a.gcode", content=body,
                  headers={HEADER: "1", "Origin": "https://evil.example"}).status_code == 403
    assert TestClient(shell.api, base_url="http://evil.example:8600").get("/check/api/jobs").status_code == 400
    r = upload(c, "notes.txt", b"hello")
    assert r.status_code == 400 and "drop a CAD file" in r.json()["error"]
    assert upload(c, "empty.gcode", b"").status_code == 400


def test_upload_names_are_made_safe():
    assert safe_name("..\\..\\evil/../x.stl") == "x.stl"
    assert safe_name("bad<>:name.gcode") == "bad_name.gcode"
    assert file_kind("Part.GCODE.3MF") == "gcode" and file_kind("part.3mf") == "cad" and file_kind("a.pdf") is None


def test_make_a_loop_file_from_a_checked_plate(c):
    plate = wait(c, upload(c, "bracket_plate.gcode.3mf", (EX / "bracket_plate.gcode.3mf").read_bytes()).json()["id"])
    assert plate["status"] == "done" and plate["can_loop"]
    r = c.post("/check/api/loop", json={"job": plate["id"], "loops": 3}, headers=WINDOW)
    assert r.status_code == 201, r.text
    job = wait(c, r.json()["id"])
    assert job["status"] == "done", job["error"]
    assert any("loop file by ProofLoop: 3 loops" in s for s in job["summary"]) and not job["can_loop"]
    made = c.get(job["download"])
    assert made.status_code == 200 and made.content[:2] == b"PK"


def test_loop_needs_a_plate_and_the_window(c):
    r = c.post("/check/api/loop", json={"job": "nope", "loops": 3}, headers=WINDOW)
    assert r.status_code == 400 and "no Bambu plate" in r.json()["error"]
    assert c.post("/check/api/loop", json={"job": "nope", "loops": 3}).status_code == 403


def test_long_jobs_send_a_notification(c, monkeypatch):
    from proofloop.app import checks
    from proofloop.core import notify

    sent = []
    monkeypatch.setattr(notify, "send", lambda title, msg, link=None: sent.append((title, msg)) or 1)
    monkeypatch.setattr(checks, "NOTIFY_AFTER_S", 0)
    wait(c, upload(c, "bracket_good.gcode", (EX / "bracket_good.gcode").read_bytes()).json()["id"])
    time.sleep(0.2)
    assert sent and sent[0][0] == "ProofLoop: bracket_good.gcode is done"


def test_plates_handed_over_between_tabs(c, shell):
    plate = EX / "bracket_plate.gcode.3mf"
    job = shell.checks.submit_bytes(plate.name, plate.read_bytes())
    assert wait(c, job.id)["can_loop"]
    assert shell.checks.job_source(job.id) == (plate.read_bytes(), plate.name)
    with pytest.raises(ValueError):
        shell.checks.submit_bytes("empty.gcode.3mf", b"")
    flat = shell.checks.submit_bytes("bracket_flawed.gcode", (EX / "bracket_flawed.gcode").read_bytes())
    wait(c, flat.id)
    with pytest.raises(ValueError):
        shell.checks.job_source(flat.id)  # plain G-code: nothing to make a loop file from


def test_jobs_are_kept_after_proofloop_closes(c, shell):
    job = wait(c, upload(c, "bracket_flawed.gcode", (EX / "bracket_flawed.gcode").read_bytes()).json()["id"])
    again = Checks(shell.checks.settings, shell.checks.db)  # ProofLoop opened again
    assert job["id"] in again.jobs and again.jobs[job["id"]].status == "done"
    assert again.jobs[job["id"]].findings and again.jobs[job["id"]].printer
    again.close()
    assert c.post("/check/api/remove", json={"job": job["id"]}, headers=WINDOW).status_code == 200
    assert job["id"] not in shell.checks.jobs
    third = Checks(shell.checks.settings, shell.checks.db)
    assert job["id"] not in third.jobs
    third.close()


def test_false_alarms_count_lower_next_time(c):
    body = (EX / "bracket_flawed.gcode").read_bytes()
    job = wait(c, upload(c, "bracket_flawed.gcode", body).json()["id"])
    top = next(f for f in job["findings"] if f["severity"] >= 2)
    for _ in range(2):
        r = c.post("/check/api/feedback", json={"job": job["id"], "check": top["check"], "verdict": "wrong"},
                   headers=WINDOW)
    assert r.status_code == 200 and r.json() == {"wrong": 2, "right": 0}
    again = wait(c, upload(c, "bracket_flawed.gcode", body).json()["id"])
    now = next(f for f in again["findings"] if f["check"] == top["check"])
    assert now["severity"] == top["severity"] - 1
    r = c.post("/check/api/feedback", json={"job": job["id"], "check": "nope", "verdict": "wrong"}, headers=WINDOW)
    assert r.status_code == 400


def test_your_saved_printers_come_first(c, tmp_path):
    (tmp_path / "home" / "settings.json").write_text(json.dumps({"printers": [
        {"name": "Farm 1", "model_id": "N7"}, {"name": "Big one", "model_id": "O1D"}]}), encoding="utf-8")
    page = c.get("/check").text
    assert '<optgroup label="Your printers">' in page
    assert "Farm 1 (Bambu Lab P2S)" in page and "Big one (Bambu Lab H2D)" in page
    from proofloop.check.my_printers import default_printer

    assert default_printer({"printers": {"bambu_h2d": {"names": ["Bambu Lab H2D"]}, "bambu_p2s":
                                         {"names": ["Bambu Lab P2S"]}}, "default_printer": "x"},
                           tmp_path / "home") == "bambu_p2s"
