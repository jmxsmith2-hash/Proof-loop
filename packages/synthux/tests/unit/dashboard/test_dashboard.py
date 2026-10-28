import threading
import urllib.error
import urllib.request

import pytest

from synthux.core.db import utcnow
from synthux.dashboard.server import Dashboard, serve


def _get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


def test_dashboard_lists_runs_and_blocks_traversal(settings, db):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.insert("runs", {"id": "run_x", "created_at": utcnow(), "study": "demo", "target_url": "http://x",
                       "status": "done", "config_json": {}, "calibration_version": "default"})
    (settings.data_dir / "reports" / "run_x").mkdir(parents=True)
    (settings.data_dir / "reports" / "run_x" / "index.html").write_text("<h1>report</h1>")
    (settings.data_dir / "secret.txt").write_text("nope")
    server = serve(settings, db, 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body = _get(f"http://127.0.0.1:{port}/")
        assert status == 200 and "run_x" in body and "SYNTHETIC DATA" in body
        assert _get(f"http://127.0.0.1:{port}/reports/run_x/index.html") == (200, "<h1>report</h1>")
        assert _get(f"http://127.0.0.1:{port}/reports/..%2Fsecret.txt")[0] == 403
        assert _get(f"http://127.0.0.1:{port}/run/run_missing")[0] == 404
    finally:
        server.shutdown()


def test_dashboard_refuses_public_bind(settings, db):
    with pytest.raises(ValueError):
        serve(settings, db, 0, host="0.0.0.0")
    assert Dashboard(settings, db).runs() == []
