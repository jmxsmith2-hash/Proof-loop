import threading
import urllib.error
import urllib.request

import pytest

from synthux.core.db import utcnow
from synthux.dashboard.server import Dashboard, serve


def _get(url):
    try:
        with urllib.request.urlopen(url) as r:
            data = r.read()
            return r.status, data.decode() if r.headers.get_content_type().startswith("text/") else data
    except urllib.error.HTTPError as e:
        return e.code, ""


def test_dashboard_lists_runs_and_blocks_traversal(settings, db):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.insert("runs", {"id": "run_x", "created_at": utcnow(), "study": "demo", "target_url": "http://x",
                       "status": "done", "config_json": {}, "calibration_version": "default"})
    (settings.data_dir / "reports" / "run_x").mkdir(parents=True)
    (settings.data_dir / "reports" / "run_x" / "index.html").write_text("<h1>report</h1>", encoding="utf-8")
    (settings.data_dir / "secret.txt").write_text("nope", encoding="utf-8")
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


def test_session_viewer_shows_steps_and_serves_only_run_screenshots(settings, db):
    from PIL import Image

    run_dir = settings.data_dir / "runs" / "run_y" / "ses_1"
    run_dir.mkdir(parents=True)
    Image.new("RGB", (40, 30), "white").save(run_dir / "000.jpg")
    (settings.data_dir / "secret.txt").write_text("nope", encoding="utf-8")
    db.insert("runs", {"id": "run_y", "created_at": utcnow(), "study": "demo", "target_url": "http://x",
                       "status": "done", "config_json": {}, "calibration_version": "default"})
    db.insert("sessions", {"id": "ses_1", "run_id": "run_y", "persona_id": "per_1", "task_id": "signup",
                           "repeat_index": 0, "visit_index": 0, "seed": 1, "conditions_json": {"device": "mobile"},
                           "outcome": "quit", "quit_reason": "I give up", "steps_count": 1, "sim_seconds": 1,
                           "wall_seconds": 1, "final_frustration": 70, "memory_json": {}, "started_at": "t",
                           "ended_at": "t"})
    db.insert("steps", {"session_id": "ses_1", "idx": 0, "screenshot_path": str(run_dir / "000.jpg"),
                        "screen_hash": "0", "screen_id": "/signup", "url": "http://x/signup",
                        "think_json": {"sees": "a form", "expects": "ok", "does": "I click 'Sign up'", "feels": "fine"},
                        "action_json": {"kind": "click", "x": 9, "y": 9}, "intended_json": {"kind": "click", "x": 5,
                                                                                             "y": 5, "target": "Sign up"},
                        "signals_json": {"no_change": True, "noise_applied": True}, "frustration": 12,
                        "sim_seconds": 1})
    server = serve(settings, db, 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body = _get(f"http://127.0.0.1:{port}/run/run_y/sessions")
        assert status == 200 and "ses_1" in body and "mobile" in body
        status, body = _get(f"http://127.0.0.1:{port}/session/ses_1")
        assert status == 200 and "I click &#39;Sign up&#39;" in body and "simulated slip: misclick" in body
        assert "/shots/run_y/ses_1/000.jpg" in body
        assert _get(f"http://127.0.0.1:{port}/shots/run_y/ses_1/000.jpg")[0] == 200
        assert _get(f"http://127.0.0.1:{port}/shots/..%2Fsecret.txt")[0] == 403
        assert _get(f"http://127.0.0.1:{port}/session/ses_nope")[0] == 404
    finally:
        server.shutdown()
