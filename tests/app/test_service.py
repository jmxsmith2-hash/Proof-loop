import json
import socket
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from proofloop import __version__
from proofloop.app import autostart, doctor
from proofloop.app.config import Settings
from proofloop.cli import main, running


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeLink:
    def __init__(self, p):
        from proofloop.monitor.printer.state import PrinterState

        self.state = PrinterState(online=True, gcode_state="RUNNING")

    def add_listener(self, fn):
        pass

    def connect(self):
        pass


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path / "proofloop"))
    return tmp_path / "proofloop"


def make(tmp_path, port=None, printers=0):
    from proofloop.app.service import Service
    from proofloop.core.paths import home as home_dir
    from proofloop.core.settings import PrinterConfig
    from proofloop.monitor.service import App

    if printers:  # add printers the way the app would, with a fake connection
        app = App(home_dir(), connect=False, link_factory=FakeLink)
        for i in range(printers):
            app.add_printer(PrinterConfig(name=f"p2s-{i}", host="127.0.0.1", serial=f"0{i}", access_code="12345678",
                                          model_id="N7"))
        app.shutdown()
    return Service(port or free_port(), connect=False, link_factory=FakeLink)


def window(svc):
    """A client like the ProofLoop window, and the headers its requests carry."""
    return TestClient(svc.shell.api, base_url=svc.shell.origin), {"X-ProofLoop": "1", "Origin": svc.shell.origin}


def events(svc):
    return [e for e in svc.shell.monitor.store.events() if e["kind"] == "proofloop"]


def test_runs_in_the_background_and_says_when_it_stops(tmp_path, home):
    svc = make(tmp_path, printers=2)
    svc.start()
    try:
        assert running(svc.port)
        assert "watching 2 printers" in svc.summary() and "2 printing" in svc.summary()
        assert (home / "running.json").is_file()
        assert "watching 2 printers" in events(svc)[0]["message"]
    finally:
        svc.stop("a test")
    assert not running(svc.port) and not (home / "running.json").exists()
    msg = events(svc)[0]["message"]
    assert "stopped (a test)" in msg and "not being watched" in msg and "Still printing: p2s-0, p2s-1" in msg


def test_says_how_long_printers_went_unwatched_after_a_crash(tmp_path, home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "running.json").write_text(json.dumps({"pid": 1, "beat": time.time() - 3 * 3600}), encoding="utf-8")
    svc = make(tmp_path)
    svc.start()
    try:
        msg = events(svc)[0]["message"]
        assert "unexpected stop" in msg and "about 3 h 0 min" in msg
        assert events(svc)[0]["level"] == "warning"  # sent to your phone, not only logged
    finally:
        svc.stop()


def test_window_controls_need_the_window(tmp_path, home):
    svc = make(tmp_path)
    c, WINDOW = window(svc)
    try:
        assert c.get("/proofloop/status").status_code == 403  # holds the phone code
        assert c.post("/proofloop/quit", json={}).status_code == 403
        assert c.post("/proofloop/phone", json={"on": True}, headers={"Origin": "https://example.com", "X-ProofLoop": "1"}
                      ).status_code == 403
        st = c.get("/proofloop/status", headers=WINDOW).json()
        assert st["background"] and st["phone"]["on"] is False and st["version"] == __version__
        page = c.get("/proofloop/doctor")
        assert page.status_code == 200 and "Setup check" in page.text and "No printers added yet" in page.text
    finally:
        svc.shell.close()


def test_phone_pairs_with_the_code_or_the_qr_link_and_sees_only_loop_files_and_printers(tmp_path, home):
    svc = make(tmp_path)
    svc.settings.phone_port = free_port()
    c, WINDOW = window(svc)
    try:
        st = c.post("/proofloop/phone", json={"on": True}, headers=WINDOW).json()["phone"]
        assert st["on"] and st["code"] and "<svg" in st["qr"] and st["pair_url"].endswith(svc.settings.phone_secret)
        assert Settings.load().phone  # remembered for next time
        phone = TestClient(svc.phone.app, base_url="http://192.168.1.20:8601")
        assert "Pair this phone" in phone.get("/").text  # not paired yet: the pairing page only
        assert phone.get("/api/state").status_code == 401
        assert phone.post("/pair", data={"code": "000000" if st["code"] != "000000" else "111111"}).status_code == 403
        r = phone.post("/pair", data={"code": st["code"]}, follow_redirects=False)
        assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
        assert phone.get("/api/state").status_code == 200  # paired: ClearLoop works
        assert phone.get("/proofloop/status").status_code in (401, 404)  # ProofLoop's own controls never
        assert phone.post("/proofloop/to-check", json={}).status_code in (401, 404, 405)
        other = TestClient(svc.phone.app, base_url="http://192.168.1.21:8601")
        assert other.get(f"/pair?key={svc.settings.phone_secret}", follow_redirects=False).status_code == 303
        assert other.get("/pair?key=wrong").status_code == 403
        # forgetting unpairs every phone
        c.post("/proofloop/phone", json={"forget": True, "on": True}, headers=WINDOW)
        assert phone.get("/api/state").status_code == 401
        # over the network for real
        with urllib.request.urlopen(f"http://127.0.0.1:{svc.settings.phone_port}/", timeout=5) as resp:
            assert b"Pair this phone" in resp.read()
    finally:
        svc.phone.stop()
        svc.shell.close()


def test_phone_codes_cant_be_guessed(tmp_path, home):
    svc = make(tmp_path)
    phone = TestClient(svc.phone.app, base_url="http://192.168.1.20:8601")
    try:
        wrong = "000000" if svc.settings.phone_code != "000000" else "111111"
        for _ in range(5):
            assert phone.post("/pair", data={"code": wrong}).status_code == 403
        assert phone.post("/pair", data={"code": svc.settings.phone_code}).status_code == 429
    finally:
        svc.shell.close()


def test_start_at_login(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert not autostart.enabled()
    path = autostart.enable()
    text = path.read_text(encoding="utf-8")
    assert autostart.enabled() and "-m\" \"proofloop\" \"service\"" in text and "Path=" in text
    autostart.disable()
    assert not autostart.enabled()
    monkeypatch.setattr(autostart.sys, "platform", "darwin")
    monkeypatch.setattr(autostart.Path, "home", lambda: tmp_path)
    plist = autostart.enable().read_text(encoding="utf-8")
    assert "<string>service</string>" in plist and "<key>RunAtLoad</key><true/>" in plist


def test_setup_check(tmp_path, home, monkeypatch, capsys):
    results = doctor.checks(None)
    titles = [r["title"] for r in results]
    assert any(t.startswith(f"ProofLoop {__version__}") for t in titles) and "Your settings and data" in titles
    assert "Advanced settings: all built-in" in titles
    assert "No printers added yet" in titles and "Sweep floor not calibrated" in titles
    assert any("Doesn't start when you log in" in t for t in titles)
    assert any(r["detail"] for r in results if r["level"] != doctor.OK)  # every problem says what to do
    assert main(["doctor"]) in (0, 1)
    assert "Setup check" not in capsys.readouterr().out  # the terminal gets text, not the web page


def test_command_line_without_a_running_proofloop(capsys):
    port = str(free_port())
    assert main(["status", "--port", port]) == 1
    assert main(["phone", "on", "--port", port]) == 2
    assert "isn't running" in capsys.readouterr().err
    assert main(["stop", "--port", port]) == 0


def test_update_without_git(tmp_path, monkeypatch):
    from proofloop.app import update

    monkeypatch.setattr(update, "repo_root", lambda: tmp_path)
    said = []
    assert update.update(say=said.append) == 2 and "wasn't installed with git" in said[0]


def test_tray_icon_draws_without_a_desktop():
    from proofloop.app.tray import icon_image

    img = icon_image(64)
    assert img.size == (64, 64) and img.getpixel((32, 2))[3] > 0


def test_setup_check_prints_on_a_windows_console(tmp_path, home, monkeypatch):
    """cp1252 (old Windows consoles, pipes) has no ✓: the output is still written, not a crash."""
    import io
    import sys

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    assert main(["doctor"]) in (0, 1)
    sys.stdout.flush()
    assert "✓".encode() in raw.getvalue()


def test_finds_its_own_folder():
    from pathlib import Path

    root = autostart.repo_root()
    assert root is not None and (root / "src" / "proofloop").is_dir() and (root / "setup.bat").is_file()
    assert root == Path(__file__).resolve().parents[2]


def test_the_window_shows_whats_new(tmp_path):
    from fastapi.testclient import TestClient

    from proofloop.app.shell import Shell

    n = TestClient(Shell(8697, connect=False).api, base_url="http://127.0.0.1:8697").get("/proofloop/changelog").json()
    assert n["version"] == proofloop_version() and n["sections"][0]["version"] == n["version"]


def proofloop_version():
    import proofloop

    return proofloop.__version__
