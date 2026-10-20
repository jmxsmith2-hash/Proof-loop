import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from helpers import box_gcode, make_3mf
from test_loop import FakeLink

from clearloop.config import Config
from clearloop.farm import Farm
from clearloop.server import create_app


@pytest.fixture
def farm(tmp_path):
    lib = tmp_path / "keychain.gcode.3mf"
    lib.write_bytes(make_3mf(box_gcode([(50, 50, 90, 70, 3.0)])))
    cfg = Config.model_validate(
        {
            "farm": {
                "data_dir": str(tmp_path / "data"),
                "secret": "farm-secret",
                "shopify_webhook_secret": "shh",
                "api_tokens": [
                    {"token": "view", "role": "viewer"},
                    {"token": "op", "role": "operator"},
                    {"token": "adm", "role": "admin"},
                ],
            },
            "printers": [
                {
                    "name": "p1",
                    "model": "p1s",
                    "host": "10.0.0.2",
                    "serial": "S1",
                    "access_code": "12345678",
                    "ejector": {"kind": "gcode"},
                }
            ],
            "skus": [{"sku": "KEY-1", "file": str(lib), "parts_per_plate": 10}],
        }
    )
    f = Farm(cfg, links={"p1": FakeLink()}, connect=False)
    f.controllers["p1"].on_state(f.links["p1"].state)
    return f, lib


def auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_health_and_roles(farm):
    f, _ = farm
    c = TestClient(create_app(f))
    assert c.get("/api/health").json()["auth"] is True
    assert c.get("/api/printers").status_code == 401
    assert c.get("/api/printers", headers=auth("view")).json()[0]["phase"] == "idle"
    assert c.post("/api/printers/p1/cleared", headers=auth("view")).status_code == 403
    assert c.post("/api/printers/p1/cleared", headers=auth("op")).json()["bed_clear"] is True
    assert c.get("/", headers={}).status_code == 200


def test_upload_to_queue_and_cancel(farm):
    f, lib = farm
    c = TestClient(create_app(f))
    r = c.post(
        "/api/queue",
        headers=auth("op"),
        files={"file": ("keychain.gcode.3mf", lib.read_bytes())},
        data={"copies": "2", "models": "p1s"},
    )
    assert r.status_code == 200, r.text
    ids = r.json()["jobs"]
    q = c.get("/api/queue", headers=auth("view")).json()
    assert {j["id"] for j in q} == set(ids) and q[0]["materials"] == ["PLA"]
    assert c.delete(f"/api/queue/{ids[0]}", headers=auth("op")).json() == {"cancelled": ids[0]}


def test_processing_api(farm):
    f, lib = farm
    c = TestClient(create_app(f))
    r = c.post(
        "/api/process",
        headers=auth("op"),
        files={"file": ("k.gcode.3mf", lib.read_bytes())},
        data={"printer": "p1", "copies": "2"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["x-clearloop-eject-mode"] == "gcode"
    assert r.content[:2] == b"PK"
    assert json.loads(r.headers["x-clearloop-cost-per-part"])["per_part"] > 0


def test_shopify_webhook(farm):
    f, _ = farm
    c = TestClient(create_app(f))
    body = json.dumps(
        {"name": "#1001", "line_items": [{"sku": "KEY-1", "quantity": 25}, {"sku": "NOPE", "quantity": 1}]}
    ).encode()
    bad = c.post("/api/orders/shopify", content=body, headers={"X-Shopify-Hmac-Sha256": "nope"})
    assert bad.status_code == 401
    sig = base64.b64encode(hmac.new(b"shh", body, hashlib.sha256).digest()).decode()
    r = c.post("/api/orders/shopify", content=body, headers={"X-Shopify-Hmac-Sha256": sig})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["queued"][0]["plates"] == 3  # 25 parts at 10 per plate
    assert out["unknown_skus"] == ["NOPE"]
    assert len(f.store.jobs("queued")) == 3


def test_farm_prepares_loop_ready_job(farm):
    f, lib = farm
    [jid] = f.enqueue_file(lib)
    ctl = f.controllers["p1"]
    remote, data, plate = f.prepare_job(f.cfg.printer("p1"), ctl.ejector, f.store.job(jid))
    assert remote.startswith(f"cl-{jid}-") and remote.endswith(".gcode.3mf") and plate == 1
    name, gcode = f.recovery_job(f.cfg.printer("p1"), ctl.ejector, f.store.job(jid))
    assert b"CLEARLOOP_MARKER SWEEP_DONE" in gcode and b"wipe" in gcode
