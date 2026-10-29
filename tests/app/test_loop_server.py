import io

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.loopfiles import threemf
from proofloop.monitor.printer.state import PrinterState
from proofloop.monitor.service import App
from tests.plates import BUCKET, farmloop_3mf, make_3mf, plain, plate_gcode


class FakeLink:
    def __init__(self, p):
        self.state = PrinterState(online=True)
        self.listeners = []

    def add_listener(self, fn):
        self.listeners.append(fn)

    def connect(self):
        pass

    def snapshot(self):
        return b"\xff\xd8jpeg"

    def resume(self):
        return True


def client(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    return TestClient(create_app(app)), app


def up(c, data, name="bucket.gcode.3mf", url="/api/plates"):
    return c.post(url, files={"file": (name, io.BytesIO(data))})


def test_make_flow(tmp_path):
    c, _ = client(tmp_path)
    assert c.get("/").status_code == 200
    r = up(c, plain(BUCKET)).json()
    assert r["printer"] == "P2S" and r["ok"] and r["profile"]["id"] == "p2s-stage2-farmboard"
    r2 = c.post(f"/api/plates/{r['token']}/analyze", json={"loops": 3, "bender_fitted": False}).json()
    assert [s["key"] for s in r2["steps"]].count("bend") == 0
    d = c.post(f"/api/plates/{r['token']}/download", json={"loops": 3})
    assert d.headers["x-proofloop-filename"] == "FL_S2_bucket.gcode.3mf"
    assert threemf.loads(d.content).gcode.count("; EXECUTABLE_BLOCK_START") == 3



def test_open_a_plate_another_tool_put_in_the_cache(tmp_path):
    c, app = client(tmp_path)
    data = plain(BUCKET)
    token = app.uploads.put(data, "bucket.gcode.3mf")
    assert app.uploads.raw(token) == (data, "bucket.gcode.3mf")
    r = c.post(f"/api/plates/{token}/open").json()
    assert r["token"] == token and r["printer"] == "P2S" and r["profile"]["id"] == "p2s-stage2-farmboard"
    assert c.post("/api/plates/nope/open").status_code == 404

def test_friendly_errors(tmp_path):
    c, _ = client(tmp_path)
    r = up(c, b"not a zip", "x.gcode")
    assert r.status_code == 422 and "Export plate sliced file" in r.json()["detail"]
    r = up(c, make_3mf(plate_gcode(BUCKET), BUCKET, model_id="N1")).json()
    assert r["no_profile"] and r["printer"] == "A1 mini"
    r = up(c, farmloop_3mf(BUCKET))
    assert r.status_code == 422 and "FarmLoop app" in r.json()["detail"]


def test_check_import_defaults_calibration(tmp_path):
    c, app = client(tmp_path)
    f = up(c, farmloop_3mf(BUCKET), "FL_S2_b.gcode.3mf", "/api/check").json()
    assert any(x["level"] == "danger" for x in f["findings"])
    r = c.post(
        "/api/profiles/import",
        files={
            "plain": ("a.gcode.3mf", io.BytesIO(plain())),
            "farmloop": ("FL_S2_a.gcode.3mf", io.BytesIO(farmloop_3mf())),
        },
        data={"name": "Shop P2S"},
    ).json()
    assert r["profile"]["id"] == "shop-p2s" and r["dropped"]
    assert any(p["id"] == "shop-p2s" for p in c.get("/api/state").json()["profiles"])
    assert c.put("/api/defaults", json={"floor_z": 2.5, "floor_calibrated": True}).json()["floor_z"] == 2.5
    assert app.settings_store.load().defaults.floor_calibrated
    cal = c.post(
        "/api/calibration",
        files={"file": ("t.gcode.3mf", io.BytesIO(plain()))},
        data={"test": "sweep-floor", "profile_id": "p2s-sweep-only"},
    )
    assert cal.headers["x-proofloop-filename"].startswith("FL_S1_CALIBRATION-sweep-floor")


def test_monitor_api(tmp_path):
    c, app = client(tmp_path)
    r = c.post(
        "/api/monitor/printers",
        json={"name": "Left", "host": "10.0.0.2", "serial": "S", "access_code": "1", "model_id": "N7"},
    )
    assert r.status_code == 200
    assert (
        c.post(
            "/api/monitor/printers",
            json={"name": "Bad", "host": "h", "serial": "s", "access_code": "a", "model_id": "XX"},
        ).status_code
        == 422
    )
    assert c.get("/api/monitor/Left/snapshot").content.startswith(b"\xff\xd8")
    assert c.get("/api/state").json()["monitor"][0]["name"] == "Left"
    assert c.delete("/api/monitor/printers/Left").status_code == 200
    assert app.settings_store.load().printers == []


def test_app_token(tmp_path):
    c, app = client(tmp_path)
    app.settings.app_token = "secret"
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_per_part_push_is_used_but_never_saved(tmp_path):
    c, app = client(tmp_path)
    r = up(c, plain(BUCKET)).json()
    body = {"push_offset": 20, "part_push": {"104": 3}}
    a = c.post(f"/api/plates/{r['token']}/analyze", json=body).json()
    assert a["plan"]["part_target"] == {"93": 150.0, "104": 3.0} and a["plan"]["part_custom"] == ["104"]
    d = c.post(f"/api/plates/{r['token']}/download", json=body)
    assert "G1 Z150.00" in threemf.loads(d.content).gcode
    c.put("/api/defaults", json=body)
    saved = app.settings_store.load().defaults
    assert saved.push_offset == 20 and "part_push" not in saved.model_dump()
    assert c.post(f"/api/plates/{r['token']}/analyze", json={"part_push": {"104": "x"}}).status_code == 422


def test_uploads_are_kept_on_disk_and_survive_a_restart(tmp_path):
    from proofloop.monitor.service import UploadCache

    cache = UploadCache(2, tmp_path / "uploads")
    a = cache.put(b"one", "a.gcode.3mf")
    b = cache.put(b"two", "b.gcode.3mf")
    again = UploadCache(2, tmp_path / "uploads")  # a restart
    assert again.raw(a) == (b"one", "a.gcode.3mf") and again.raw(b) == (b"two", "b.gcode.3mf")
    again.put(b"three", "c.gcode.3mf")  # only the last two are kept
    assert len(list((tmp_path / "uploads").glob("*.3mf"))) == 2
    for bad in ("../../etc/passwd", "", a + "/x"):
        try:
            again.raw(bad)
            raise AssertionError("expected KeyError")
        except KeyError:
            pass
