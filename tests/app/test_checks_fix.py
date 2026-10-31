"""Fix and slice again in the Check tab: the option, the button's route, the files it makes, and the no-slicer cases."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proofloop.app.shell import HEADER, Shell
from tests.check.test_prepare import profiles  # noqa: F401 - the fake slicer and its preset tree

REPO = Path(__file__).resolve().parents[2]
STAND = REPO / "examples" / "cad" / "phone_stand_v1.stl"    # an overhang with support off: a setting fixes it
ORIGIN = "http://127.0.0.1:8600"
WINDOW = {HEADER: "1", "Origin": ORIGIN}


@pytest.fixture()
def c(profiles, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path / "home"))
    shell = Shell(8600, config=REPO / "src" / "proofloop" / "data", connect=False)
    yield TestClient(shell.api, base_url=ORIGIN)
    shell.close()


def upload(c, name, body, **opts):
    q = "&".join(f"{k}={v}" for k, v in {"name": name, **opts}.items())
    return c.post(f"/check/api/check?{q}", content=body, headers=WINDOW)


def wait(c, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        job = next(j for j in c.get("/check/api/jobs").json() if j["id"] == job_id)
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_the_upload_form_offers_the_fix_when_there_is_a_slicer(c):
    page = c.get("/check").text
    assert "Apply the slicer fixes and slice again" in page and 'id="cad-fix"' in page
    assert "data-fix" in page                                   # the Fix and slice again button's script


def test_the_fix_option_slices_again_and_shows_what_changed(c):
    job = wait(c, upload(c, "stand.stl", STAND.read_bytes(), slice=1, fix=1).json()["id"])
    assert job["status"] == "done", job["error"]
    assert job["title"].endswith("(after the fixes)") and not job["can_fix"]
    rd = job["rounds"][0]
    assert rd["blocking_after"] < rd["blocking_before"]
    support = next(st for st in rd["applied"] if st["key"] == "enable_support")
    assert (support["preset"], support["old"], support["value"]) == ("process", "0", "1")
    assert support["label"] == "Process › Support › Enable support" and support["why"]
    assert any(s.startswith("Round 1: changed") for s in job["summary"])
    assert job["can_loop"]                                      # Make it now / Fine-tune take the fixed plate
    names = {d["label"]: d["url"] for d in job["downloads"]}
    plate = c.get(next(u for lab, u in names.items() if "plate" in lab))
    assert plate.status_code == 200 and plate.content[:2] == b"PK"
    assert "attachment" in plate.headers["content-disposition"] and "stand-fixed.gcode.3mf" in plate.headers["content-disposition"]
    preset = c.get(next(u for lab, u in names.items() if "process" in lab))
    assert preset.status_code == 200 and json.loads(preset.content)["enable_support"] == "1"
    report = c.get(job["report_url"]).text
    assert "Fixed and sliced again" in report and "Enable support" in report and "slicer-fixes/process.json" in report
    assert c.get(job["report_url"] + "slicer-fixes/process.json").status_code == 200


def test_the_button_starts_a_linked_fix_job(c, monkeypatch):
    first = wait(c, upload(c, "stand.stl", STAND.read_bytes(), slice=1).json()["id"])
    assert first["status"] == "done" and not first["rounds"] and not first["downloads"]
    assert first["can_fix"] and any(st["key"] == "enable_support" for st in first["fixes"])
    with monkeypatch.context() as m:                            # the slicer was uninstalled in the meantime
        m.delenv("PROOFLOOP_SLICER")
        m.setattr("proofloop.check.slicer._candidates", lambda: [])
        listed = next(j for j in c.get("/check/api/jobs").json() if j["id"] == first["id"])
        assert listed["needs_slicer"] and not listed["can_fix"]
        r = c.post("/check/api/fix", json={"job": first["id"]}, headers=WINDOW)
        assert r.status_code == 400 and "Install Bambu Studio" in r.json()["error"]
        assert "Install Bambu Studio" in c.get("/check").text and 'id="cad-fix"' not in c.get("/check").text
    assert c.post("/check/api/fix", json={"job": first["id"]}).status_code == 403          # window only
    assert c.post("/check/api/fix", json={"job": first["id"]},
                  headers={HEADER: "1", "Origin": "https://evil.example"}).status_code == 403
    r = c.post("/check/api/fix", json={"job": first["id"]}, headers=WINDOW)
    assert r.status_code == 201 and r.json()["fix_of"] == first["id"]
    fixed = wait(c, r.json()["id"])
    assert fixed["status"] == "done", fixed["error"]
    assert fixed["rounds"][0]["applied"] and fixed["downloads"] and fixed["can_loop"]
    again = next(j for j in c.get("/check/api/jobs").json() if j["id"] == first["id"])
    assert again["fixed_by"] == fixed["id"] and not again["can_fix"]
    r = c.post("/check/api/fix", json={"job": first["id"]}, headers=WINDOW)
    assert r.status_code == 400 and "already" in r.json()["error"]
    assert c.post("/check/api/fix", json={"job": "nope"}, headers=WINDOW).status_code == 400
    # the fixed plate goes on to Make a loop file
    loop = c.post("/check/api/loop", json={"job": fixed["id"], "loops": 2}, headers=WINDOW)
    assert loop.status_code == 201 and wait(c, loop.json()["id"])["status"] == "done"


def test_a_file_that_is_already_sliced_says_it_needs_the_model(c):
    body = (REPO / "examples" / "gcode" / "bracket_flawed.gcode").read_bytes()
    job = wait(c, upload(c, "bracket_flawed.gcode", body).json()["id"])
    assert job["status"] == "done" and job["fixes"] and job["needs_model"] and not job["can_fix"]
    assert "needs the 3D model" in c.get(job["report_url"]).text
    assert c.post("/check/api/fix", json={"job": job["id"]}, headers=WINDOW).status_code == 400
