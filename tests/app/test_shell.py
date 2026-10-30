import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proofloop import __version__
from proofloop.app.shell import HEADER, Shell
from proofloop.cli import main

REPO = Path(__file__).resolve().parents[2]
PLATE = REPO / "examples" / "gcode" / "bracket_plate.gcode.3mf"
ORIGIN = "http://127.0.0.1:8600"
WINDOW = {HEADER: "1", "Origin": ORIGIN}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """ProofLoop's folder in tmp_path (settings, printers, reports)."""
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture()
def shell(home):
    s = Shell(8600, connect=False)
    yield s
    s.close()


def client(s):
    return TestClient(s.api, base_url=ORIGIN)


def wait(s, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = s.checks.jobs[job_id]
        if job.status in ("done", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_one_window_with_every_part(shell):
    c = client(shell)
    r = c.get("/")
    assert r.status_code == 200 and r.headers["x-frame-options"] == "DENY"
    assert '"check": "/check"' in r.text and '"loop": "/loop?proofloop=1"' in r.text
    for tab in (">Check<", ">Loop files<", ">Printers<", "Setup check"):
        assert tab in r.text
    assert "Website tests" not in r.text
    assert "Drop a CAD file here" in c.get("/check").text  # Check, served by the same window
    page = c.get("/loop?proofloop=1")
    assert page.status_code == 200 and "Make a loop file" in page.text and "toCheck" in page.text
    assert c.get("/api/state").json()["version"]  # the Loop files & Printers API, served by the same window
    assert c.get("/proofloop/ping").json() == {"proofloop": __version__, "background": False}


def test_only_the_window_may_hand_plates_over(shell):
    c = client(shell)
    assert c.post("/proofloop/to-loop", json={"job": "x"}).status_code == 403
    evil = {HEADER: "1", "Origin": "https://example.com"}
    assert c.post("/proofloop/to-check", json={"token": "x"}, headers=evil).status_code == 403
    other_host = TestClient(shell.api, base_url="http://evil.example:8600")
    assert other_host.get("/").status_code == 400  # DNS rebinding: only localhost names
    assert c.post("/proofloop/to-check", content=b"[1]", headers=WINDOW).status_code == 400


def test_checked_plate_opens_in_make_a_loop_file(shell):
    job = shell.checks.submit_bytes(PLATE.name, PLATE.read_bytes())
    assert wait(shell, job.id).status == "done"
    c = client(shell)
    r = c.post("/proofloop/to-loop", json={"job": job.id}, headers=WINDOW)
    assert r.status_code == 200 and r.json()["file"] == PLATE.name
    opened = c.post(f"/api/plates/{r.json()['token']}/open").json()
    assert opened["printer"] == "P2S" and opened["parts"]
    assert c.post("/proofloop/to-loop", json={"job": "nope"}, headers=WINDOW).status_code == 400


def test_plate_in_make_a_loop_file_gets_the_full_print_check(shell):
    c = client(shell)
    up = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()
    r = c.post("/proofloop/to-check", json={"token": up["token"]}, headers=WINDOW)
    assert r.status_code == 200 and r.json()["title"] == PLATE.name
    job = wait(shell, r.json()["job"])
    assert job.status == "done" and job.report
    assert c.post("/proofloop/to-check", json={"token": "gone"}, headers=WINDOW).status_code == 404


def test_command_line(capsys, tmp_path, home, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["check", str(REPO / "examples" / "gcode" / "bracket_good.gcode.3mf")]) == 0
    assert "no problems found in the G-code" in capsys.readouterr().out
    cad = REPO / "examples" / "cad"
    # It ran and found the planted flaws: exit 1 (2 would mean it couldn't check).
    assert main(["check", str(cad / "phone_stand_v1.stl"), "--design", str(cad / "phone_stand.yaml")]) == 1
    assert "Phone stand" in capsys.readouterr().out
    assert main(["loop", "profiles"]) == 0
    assert "p2s" in capsys.readouterr().out.lower()
    assert main(["check", "notes.txt"]) == 2
    with pytest.raises(SystemExit):
        main(["--version"])
