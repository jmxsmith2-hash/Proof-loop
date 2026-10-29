import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from proofloop.core import notify


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


def _targets(home, *targets):
    (home / "settings.json").write_text(json.dumps({"notifications": list(targets)}), encoding="utf-8")


def test_finished_checks_go_to_every_target(inbox, monkeypatch, tmp_path):
    url, got = inbox
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    _targets(tmp_path, {"kind": "ntfy", "url": url + "/ntfy-topic", "min_level": "error"},
             {"kind": "discord", "url": url + "/d"}, {"kind": "webhook", "url": url + "/w", "token": "t0k"})
    assert notify.send("ProofLoop: bracket ✓ is done", "2 major problems", "http://x") == 3
    by = {p: (h, b) for p, h, b in got}
    assert by["/ntfy-topic"][0]["Title"] == "ProofLoop: bracket ? is done" and by["/ntfy-topic"][1] == b"2 major problems"
    assert by["/ntfy-topic"][0]["Click"] == "http://x"
    assert json.loads(by["/d"][1]) == {"content": "**ProofLoop: bracket ✓ is done**\n2 major problems"}
    hook = json.loads(by["/w"][1])
    assert hook["title"] == "ProofLoop: bracket ✓ is done" and hook["link"] == "http://x"
    assert by["/w"][0]["Authorization"] == "Bearer t0k"


def test_nothing_set_up_or_unreachable_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    assert notify.send("T", "M") == 0
    _targets(tmp_path, {"kind": "ntfy", "url": "http://127.0.0.1:9/nothing-here"})
    assert notify.send("T", "M") == 0
