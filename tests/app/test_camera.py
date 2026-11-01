"""The camera view, the camera settings and the timelapses, over the Loop files & Printers API."""

import io
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proofloop.app.loop_server import create_app
from proofloop.app.routes.camera import Frames
from proofloop.core.settings import CameraSettings, PrinterConfig
from proofloop.monitor.service import App
from tests.app.test_loop_server import FakeLink
from tests.monitor.test_timelapse import picture


def shot(n):
    return picture(n, (320, 180))


class CameraLink(FakeLink):
    def __init__(self, p):
        super().__init__(p)
        self.taken = 0
        self.broken = False

    def snapshot(self):
        self.taken += 1
        time.sleep(0.05)  # a camera takes a moment
        if self.broken:
            raise OSError("timed out")
        return shot(self.taken)


def client(tmp_path):
    app = App(tmp_path, connect=False, link_factory=CameraLink)
    app.add_printer(PrinterConfig(name="Left", host="1.2.3.4", serial="S1", access_code="x", model_id="N7"))
    return TestClient(create_app(app)), app


def test_the_camera_gives_the_latest_picture_and_says_not_to_keep_it(tmp_path):
    c, app = client(tmp_path)
    r = c.get("/api/monitor/Left/camera")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == shot(1)
    assert "no-store" in r.headers["cache-control"]
    assert c.get("/api/monitor/Nobody/camera").status_code == 404


def test_several_viewers_share_one_request_to_the_camera(tmp_path):
    c, app = client(tmp_path)
    link = app.watches["Left"].link
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(c.get("/api/monitor/Left/camera").status_code))
               for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == [200] * 6 and link.taken == 1


def test_a_picture_is_taken_again_when_it_is_old(tmp_path):
    frames, n = Frames(age=0.05), []
    frames.get("Left", lambda: n.append(1) or b"a")
    frames.get("Left", lambda: n.append(1) or b"b")
    time.sleep(0.08)
    assert frames.get("Left", lambda: n.append(1) or b"c") == b"c" and len(n) == 2


def test_a_camera_that_fails_is_a_502_and_a_later_try_asks_again(tmp_path):
    c, app = client(tmp_path)
    link = app.watches["Left"].link
    link.broken = True
    r = c.get("/api/monitor/Left/camera")
    assert r.status_code == 502 and "camera: timed out" in r.json()["detail"]
    link.broken = False
    assert c.get("/api/monitor/Left/camera").status_code == 200 and link.taken == 2


def test_the_camera_needs_the_access_token(tmp_path):
    c, app = client(tmp_path)
    app.settings.app_token = "secret"
    for path in ("/api/monitor/Left/camera", "/api/monitor/Left/timelapses", "/api/monitor/Left/timelapses/x",
                 "/api/camera-settings", "/api/proofs/x"):
        assert c.get(path).status_code == 401
    assert c.put("/api/camera-settings", json={}).status_code == 401
    assert c.get("/api/monitor/Left/camera", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_the_camera_settings_start_off_and_are_kept(tmp_path):
    c, app = client(tmp_path)
    s = c.get("/api/camera-settings").json()
    assert s == {"look_every_s": 30, "look_every_pct": 10, "timelapse": False, "timelapse_every_layers": 5,
                 "timelapse_keep": 5, "proof_photos": True, "proof_keep": 500}
    assert c.get("/api/state").json()["camera"] == s
    r = c.put("/api/camera-settings", json={"timelapse": True, "timelapse_every_layers": 3})
    assert r.status_code == 200 and r.json()["timelapse"] is True and r.json()["look_every_pct"] == 10
    assert r.json()["look_every_s"] == 30
    assert App(tmp_path, connect=False, link_factory=CameraLink).settings.camera.timelapse_every_layers == 3


@pytest.mark.parametrize("bad", [{"look_every_pct": 3}, {"look_every_pct": 40}, {"timelapse_keep": 0},
                                 {"timelapse_every_layers": 0}, {"timelapse_keep": 500}, {"proof_keep": 5},
                                 {"proof_keep": 50000}, {"look_every_s": 5}, {"look_every_s": 3600}])
def test_settings_out_of_range_are_refused(tmp_path, bad):
    c, app = client(tmp_path)
    assert c.put("/api/camera-settings", json=bad).status_code == 422
    assert app.settings.camera.look_every_pct == 10 and app.settings.camera.timelapse_keep == 5
    assert app.settings.camera.look_every_s == 30


def test_zero_means_only_the_empty_plate_looks(tmp_path):
    c, app = client(tmp_path)
    r = c.put("/api/camera-settings", json={"look_every_s": 0, "look_every_pct": 0}).json()
    assert r["look_every_s"] == 0 and r["look_every_pct"] == 0
    w = app.watches["Left"]
    assert w.look_every() == 0 and w.look_every_s() == 0  # the running watch follows the setting


def test_looking_by_layers_instead_of_by_time(tmp_path):
    c, app = client(tmp_path)
    assert c.put("/api/camera-settings", json={"look_every_s": 0, "look_every_pct": 5}).status_code == 200
    w = app.watches["Left"]
    assert w.look_every_s() == 0 and w.look_every() == 5
    assert c.put("/api/camera-settings", json={"look_every_s": 60}).json()["look_every_s"] == 60
    assert w.look_every_s() == 60


def test_settings_from_before_looks_by_time_keep_your_choice():
    assert CameraSettings.model_validate({"look_every_pct": 10}).look_every_s == 30  # the old default
    assert CameraSettings.model_validate({}).look_every_s == 30
    assert CameraSettings.model_validate({"look_every_pct": 0}).look_every_s == 0  # you had turned it off
    assert CameraSettings.model_validate({"look_every_pct": 5}).look_every_s == 0  # you had chosen 5%
    assert CameraSettings.model_validate({"look_every_pct": 5, "look_every_s": 60}).look_every_s == 60


def test_timelapses_are_listed_and_served(tmp_path):
    c, app = client(tmp_path)
    assert c.get("/api/monitor/Left/timelapses").json() == []
    w = app.watches["Left"]
    w.run_async = lambda f: f()
    c.put("/api/camera-settings", json={"timelapse": True, "timelapse_every_layers": 2})
    link = w.link
    link.state.gcode_state, link.state.subtask_name, link.state.total_layers = "RUNNING", "bracket", 40
    for layer in range(1, 41):
        link.state.layer_num = layer
        w.on_state(link.state)
    link.state.gcode_state = "FINISH"
    w.on_state(link.state)
    (item,) = c.get("/api/monitor/Left/timelapses").json()
    assert item["job"] == "bracket" and item["outcome"] == "finished" and item["frames"] >= 20
    assert w.as_dict()["timelapses"][0]["id"] == item["id"]  # the card shows them too
    shown = c.get(f"/api/monitor/Left/timelapses/{item['id']}")
    assert shown.status_code == 200 and shown.headers["content-type"] == f"image/{item['kind']}"
    assert "attachment" not in shown.headers.get("content-disposition", "")
    with Image.open(io.BytesIO(shown.content)) as im:
        assert im.is_animated and im.width == 320  # never made larger than the camera's own picture
    saved = c.get(f"/api/monitor/Left/timelapses/{item['id']}?download=1")
    assert saved.headers["content-disposition"] == f'attachment; filename="Left-{item["id"]}.{item["kind"]}"'
    for bad in ("nothing", "20200101-000000", "..%2F..%2Fsettings"):
        assert c.get(f"/api/monitor/Left/timelapses/{bad}").status_code == 404
    assert c.get("/api/monitor/Nobody/timelapses").status_code == 404
