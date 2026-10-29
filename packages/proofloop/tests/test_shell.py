import shutil
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from proofloop.cli import main
from proofloop.shell import HEADER, Shell

PACKAGES = Path(__file__).resolve().parents[2]
PLATE = PACKAGES / "synthux" / "examples" / "gcode" / "bracket_plate.gcode.3mf"
SYNTHUX_CONFIG = PACKAGES / "synthux" / "config"
ORIGIN = "http://127.0.0.1:8600"
WINDOW = {HEADER: "1", "Origin": ORIGIN}

pytest.importorskip("synthux")
pytest.importorskip("clearloop")


def synthux_config(tmp_path):
    """synthux's own config, with its data folder in tmp_path."""
    cfg = tmp_path / "config"
    shutil.copytree(SYNTHUX_CONFIG, cfg, ignore=shutil.ignore_patterns("settings.yaml"))
    raw = yaml.safe_load((cfg / "settings.example.yaml").read_text(encoding="utf-8")) or {}
    raw["data_dir"] = str(tmp_path / "synthux-data")
    (cfg / "settings.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    return cfg


@pytest.fixture()
def shell(tmp_path):
    s = Shell(8600, clearloop_data=tmp_path / "clearloop", synthux_config=synthux_config(tmp_path), connect=False)
    yield s
    s.close()


def client(s):
    return TestClient(s.api, base_url=ORIGIN)


def wait(s, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = s.synthux.jobs[job_id]
        if job.status in ("done", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_one_window_with_both_apps(shell):
    c = client(shell)
    r = c.get("/")
    assert r.status_code == 200 and r.headers["x-frame-options"] == "DENY"
    assert f'"synthux": "{shell.synthux_url}"' in r.text and '"clearloop": "/clearloop?proofloop=1"' in r.text
    assert "&amp; test" in r.text and "&amp; printers" in r.text
    page = c.get("/clearloop?proofloop=1")
    assert page.status_code == 200 and "Make a loop file" in page.text and "toSynthux" in page.text
    assert c.get("/api/state").json()["version"]  # ClearLoop's API, served by the same window
    assert c.get("/proofloop/ping").json() == {"proofloop": "0.1.0", "synthux": True, "clearloop": True}
    assert shell.synthux.shell == ORIGIN  # synthux lets only this window frame it


def test_only_the_window_may_hand_plates_over(shell):
    c = client(shell)
    assert c.post("/proofloop/to-clearloop", json={"job": "x"}).status_code == 403
    evil = {HEADER: "1", "Origin": "https://example.com"}
    assert c.post("/proofloop/to-synthux", json={"token": "x"}, headers=evil).status_code == 403
    other_host = TestClient(shell.api, base_url="http://evil.example:8600")
    assert other_host.get("/").status_code == 400  # DNS rebinding: only localhost names
    assert c.post("/proofloop/to-synthux", content=b"[1]", headers=WINDOW).status_code == 400


def test_plate_from_synthux_opens_in_clearloop(shell):
    job = shell.synthux.submit_bytes(PLATE.name, PLATE.read_bytes())
    assert wait(shell, job.id).status == "done"
    c = client(shell)
    r = c.post("/proofloop/to-clearloop", json={"job": job.id}, headers=WINDOW)
    assert r.status_code == 200 and r.json()["file"] == PLATE.name
    opened = c.post(f"/api/plates/{r.json()['token']}/open").json()
    assert opened["printer"] == "P2S" and opened["parts"]
    assert c.post("/proofloop/to-clearloop", json={"job": "nope"}, headers=WINDOW).status_code == 400


def test_plate_from_clearloop_gets_synthux_print_check(shell):
    c = client(shell)
    up = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()
    r = c.post("/proofloop/to-synthux", json={"token": up["token"]}, headers=WINDOW)
    assert r.status_code == 200 and r.json()["title"] == PLATE.name
    job = wait(shell, r.json()["job"])
    assert job.status == "done" and job.report
    assert c.post("/proofloop/to-synthux", json={"token": "gone"}, headers=WINDOW).status_code == 404


def test_works_with_only_one_of_them(tmp_path):
    s = Shell(8600, clearloop_data=tmp_path / "cl", connect=False, use_synthux=False)
    try:
        c = client(s)
        assert '"synthux": null' in c.get("/").text
        r = c.post("/proofloop/to-synthux", json={"token": "x"}, headers=WINDOW)
        assert r.status_code == 404 and "synthux isn't installed" in r.json()["detail"]
    finally:
        s.close()
    with pytest.raises(RuntimeError):
        Shell(8600, use_synthux=False, use_clearloop=False)


def test_command_line(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHUX_CONFIG_DIR", str(synthux_config(tmp_path)))
    monkeypatch.chdir(tmp_path)  # reports land here, not in the repository
    assert main(["check", str(PACKAGES / "synthux" / "examples" / "gcode" / "bracket_good.gcode.3mf")]) == 0
    assert "no problems found in the G-code" in capsys.readouterr().out
    cad = PACKAGES / "synthux" / "examples" / "cad"
    assert main(["check", str(cad / "phone_stand_v1.stl"), "--design", str(cad / "phone_stand.yaml")]) == 0
    assert "Phone stand" in capsys.readouterr().out
    assert main(["clearloop", "profiles"]) == 0
    assert "p2s" in capsys.readouterr().out.lower()
    assert main(["check", "notes.txt"]) == 2
    with pytest.raises(SystemExit):
        main(["--version"])
