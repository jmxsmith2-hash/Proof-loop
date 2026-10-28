import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from synthux import notify


@pytest.fixture
def inbox():
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            got.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", got
    srv.shutdown()


def test_ntfy_from_the_environment(inbox, monkeypatch):
    url, got = inbox
    monkeypatch.setenv("SYNTHUX_NOTIFY_URL", url + "/ntfy-topic")
    assert notify.send("Synthux: bracket is done", "2 major problems") == 1
    path, headers, body = got[0]
    assert path == "/ntfy-topic" and headers["Title"] == "Synthux: bracket is done" and body == b"2 major problems"


def test_clearloop_targets_are_reused(inbox, monkeypatch, tmp_path):
    url, got = inbox
    monkeypatch.delenv("SYNTHUX_NOTIFY_URL", raising=False)
    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps({"notifications": [{"kind": "discord", "url": url + "/d"},
                                                 {"kind": "webhook", "url": url + "/w", "token": "t0k"}]}),
                   encoding="utf-8")
    monkeypatch.setattr(notify, "CLEARLOOP_SETTINGS", cfg)
    assert notify.send("T", "M", "http://x") == 2
    by = {p: (h, json.loads(b)) for p, h, b in got}
    assert by["/d"][1] == {"content": "**T**\nM"}
    assert by["/w"][1]["title"] == "T" and by["/w"][0]["Authorization"] == "Bearer t0k"


def test_nothing_set_up_or_unreachable_never_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("SYNTHUX_NOTIFY_URL", raising=False)
    monkeypatch.setattr(notify, "CLEARLOOP_SETTINGS", tmp_path / "missing.json")
    assert notify.send("T", "M") == 0
    monkeypatch.setenv("SYNTHUX_NOTIFY_URL", "http://127.0.0.1:9/nothing-here")
    assert notify.send("T", "M") == 0
