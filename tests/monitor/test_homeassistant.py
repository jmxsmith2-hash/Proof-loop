"""Home Assistant / MQTT export, against a fake paho client (no broker, no network)."""

import json
import re
from types import SimpleNamespace

import pytest

from proofloop.core.settings import MqttExport, PrinterConfig
from proofloop.monitor import homeassistant as ha
from proofloop.monitor.printer.state import PrinterState
from proofloop.monitor.vision import BedCheck


class FakeClient:
    """Just enough of paho's client. Subscribing is a test failure: the export is read-only."""

    made: list = []

    def __init__(self, cfg, will=None):
        self.cfg, self.will = cfg, will
        self.sent: list = []  # (topic, payload, qos, retain)
        self.calls: list = []
        self.rc = 0
        self.on_connect = self.on_disconnect = self.on_connect_fail = None
        FakeClient.made.append(self)

    def connect_async(self, host, port, keepalive=60):
        self.calls.append(("connect_async", host, port))

    def loop_start(self):
        self.calls.append(("loop_start",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.sent.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self.rc, wait_for_publish=lambda timeout=None: None)

    def subscribe(self, *a, **k):
        raise AssertionError("the export must never subscribe")

    def up(self, reason=0):
        self.on_connect(self, None, {}, reason, None)

    def messages(self, topic_part):
        return [(t, p) for t, p, *_ in self.sent if topic_part in t]


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class Watch:
    def __init__(self, state=None):
        self.link = SimpleNamespace(state=state or PrinterState(online=True, gcode_state="RUNNING"))
        self.loop_no = 0
        self.waiting_for_you = False
        self.last_check = None


class Tracker:
    def job_for(self, state):
        return "k", {"loops": 10}


class Farm:
    def __init__(self, *names):
        self.watches = {n: Watch() for n in names}
        self.settings = SimpleNamespace(printers=[PrinterConfig(name=n, host="h", serial="s", access_code="c", model_id="N7")
                                                  for n in names])
        self.tracker = Tracker()


@pytest.fixture
def rig():
    FakeClient.made = []
    clock, farm = Clock(), Farm("P2S 1")
    exp = ha.HomeAssistantExport(farm, client_factory=FakeClient, thread=False, clock=clock)
    cfg = MqttExport(enabled=True, host="broker.local", username="u", password="p")
    exp.configure(cfg)
    client = FakeClient.made[-1]
    return exp, client, farm, clock


def states(client):
    return [json.loads(p) for t, p in client.messages("/state") if t.endswith("/state")]


def test_off_by_default_and_never_starts_without_a_broker():
    FakeClient.made = []
    farm = Farm("a")
    exp = ha.HomeAssistantExport(farm, client_factory=FakeClient, thread=False)
    exp.configure(MqttExport())
    exp.configure(MqttExport(enabled=False, host="broker"))
    assert FakeClient.made == [] and exp.status()["state"] == "off"
    with pytest.raises(ValueError):
        MqttExport(enabled=True)
    quiet = ha.HomeAssistantExport(farm, client_factory=FakeClient, active=False)
    quiet.configure(MqttExport(enabled=True, host="b"))
    assert FakeClient.made == [] and quiet.status()["state"] == "paused"


def test_connects_with_a_last_will_and_publishes_nothing_until_connected(rig):
    exp, client, _farm, _clock = rig
    assert client.will == ("proofloop/status", "offline") and ("connect_async", "broker.local", 1883) in client.calls
    exp.tick()
    assert client.sent == [] and exp.status()["state"] == "connecting"
    client.up()
    exp.tick()
    assert client.messages("/status") == [("proofloop/status", "online")]
    assert exp.status()["state"] == "connected"


def test_discovery_is_retained_and_describes_each_sensor(rig):
    exp, client, _farm, _clock = rig
    client.up()
    exp.tick()
    configs = {t: json.loads(p) for t, p in client.messages("/config")}
    assert all(r for t, p, q, r in client.sent)  # retained
    expected = {"state", "progress", "layer", "remaining", "nozzle_temp", "bed_temp", "job", "plate_check", "loop", "loops_total"}
    topics = {t for t in configs}
    assert topics == {f"homeassistant/sensor/proofloop_p2s_1/{o}/config" for o in expected} | {
        "homeassistant/binary_sensor/proofloop_p2s_1/needs_you/config"}
    for topic, cfg in configs.items():
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", topic.split("/")[2]) and re.fullmatch(r"[a-zA-Z0-9_-]+", topic.split("/")[3])
        assert cfg["state_topic"] == "proofloop/p2s_1/state" and cfg["availability_topic"] == "proofloop/status"
        assert cfg["unique_id"].startswith("proofloop_p2s_1_") and cfg["device"]["identifiers"] == ["proofloop_p2s_1"]
        assert cfg["device"]["name"] == "P2S 1" and cfg["device"]["model"] == "P2S"
        assert set(cfg).isdisjoint({"command_topic"})  # nothing that could control a printer
    assert configs["homeassistant/sensor/proofloop_p2s_1/nozzle_temp/config"]["device_class"] == "temperature"
    assert configs["homeassistant/binary_sensor/proofloop_p2s_1/needs_you/config"]["device_class"] == "problem"
    assert set(configs["homeassistant/sensor/proofloop_p2s_1/state/config"]["options"]) >= {"idle", "printing", "paused"}


def test_state_values(rig):
    exp, client, farm, _clock = rig
    w = farm.watches["P2S 1"]
    w.link.state = PrinterState(online=True, gcode_state="PAUSE", percent=42, layer_num=12, total_layers=80, remaining_min=95,
                                nozzle_temp=219.96, bed_temp=60.04, subtask_name="FL_S2_bracket.gcode.3mf")
    w.loop_no, w.waiting_for_you = 3, True
    w.last_check = BedCheck(clear=False, changed_fraction=0.1, largest_blob_px=200, reference="r")
    client.up()
    exp.tick()
    (s,) = states(client)
    assert s == {"state": "paused", "progress": 42, "layer": 12, "total_layers": 80, "remaining_min": 95, "nozzle_temp": 220.0,
                 "bed_temp": 60.0, "job": "FL_S2_bracket.gcode.3mf", "plate_check": "parts left", "loop": 3, "loops_total": 10,
                 "needs_you": True}
    w.link.state.online = False
    w.last_check = BedCheck(clear=True, changed_fraction=0, largest_blob_px=0, reference="r")
    _clock.t += 10
    exp.tick()
    assert states(client)[-1]["state"] == "offline" and states(client)[-1]["plate_check"] == "empty"
    assert ha.STATES == {"IDLE": "idle", "PREPARE": "preparing", "RUNNING": "printing", "PAUSE": "paused", "FINISH": "finished",
                         "FAILED": "failed"}


def test_publishes_on_change_at_most_once_per_interval(rig):
    exp, client, farm, clock = rig
    client.up()
    exp.tick()
    assert len(states(client)) == 1
    exp.tick()  # nothing changed
    farm.watches["P2S 1"].link.state.percent = 5
    clock.t += 2
    exp.tick()  # changed, but too soon
    assert len(states(client)) == 1
    clock.t += 4
    exp.tick()
    assert [s["progress"] for s in states(client)] == [0, 5]
    farm.watches["P2S 1"].link.state.percent = 6
    farm.watches["P2S 1"].link.state.percent = 7
    clock.t += 5
    exp.tick()
    exp.tick()
    assert [s["progress"] for s in states(client)] == [0, 5, 7]


def test_reconnecting_announces_everything_again_and_a_dead_broker_changes_nothing(rig):
    exp, client, farm, clock = rig
    client.up()
    exp.tick()
    first = len(client.sent)
    client.on_disconnect(client, None, None, 7, None)
    exp.tick()
    assert len(client.sent) == first and exp.status()["state"] == "connecting"
    client.up()
    exp.tick()
    assert len(client.sent) == 2 * first  # status, configs and state, once more
    client.rc = 4  # the client says the message wasn't taken (not connected)
    farm.watches["P2S 1"].link.state.percent = 9
    clock.t += 10
    exp.tick()
    assert exp.status()["state"] == "connected"
    client.rc = 0
    exp.tick()
    assert states(client)[-1]["progress"] == 9  # tried again, nothing lost


def test_a_broken_printer_or_publish_never_stops_the_rest(rig):
    exp, client, farm, _clock = rig
    farm.watches["bad"] = SimpleNamespace(link=None)
    farm.settings.printers.append(PrinterConfig(name="bad", host="h", serial="s", access_code="c", model_id="N7"))
    farm.watches["Second"] = Watch()
    client.up()
    exp.tick()
    assert {s["state"] for s in states(client)} == {"printing"}
    assert len(states(client)) == 2  # P2S 1 and Second; "bad" only logged
    client.publish = lambda *a, **k: (_ for _ in ()).throw(OSError("broker went away"))
    farm.watches["Second"].link.state.percent = 50
    _clock.t += 10
    exp.tick()  # raises nothing


def test_names_that_look_the_same_get_different_ids_and_removed_printers_are_cleared(rig):
    exp, client, farm, _clock = rig
    farm.watches["P2S-1"] = Watch()
    client.up()
    exp.tick()
    ids = {t.split("/")[2] for t, _p in client.messages("/config")}
    assert len(ids) == 2 and "proofloop_p2s_1" in ids
    del farm.watches["P2S-1"]
    exp.tick()
    cleared = [t for t, p, q, r in client.sent if p == b""]
    assert any("/config" in t for t in cleared) and len(cleared) == 12  # 11 sensors and the state topic


def test_stopping_says_offline_and_disconnects_and_never_subscribes(rig):
    exp, client, _farm, _clock = rig
    client.up()
    exp.tick()
    exp.configure(MqttExport(enabled=False, host="broker.local"))
    assert client.messages("/status")[-1] == ("proofloop/status", "offline")
    assert ("disconnect",) in client.calls and ("loop_stop",) in client.calls and exp.status()["state"] == "off"
    exp.configure(MqttExport(enabled=True, host="other.local", tls=True))
    assert FakeClient.made[-1] is not client and ("connect_async", "other.local", 8883) in FakeClient.made[-1].calls
    same = FakeClient.made[-1]
    exp.configure(MqttExport(enabled=True, host="other.local", tls=True))  # same settings: left alone
    assert FakeClient.made[-1] is same


def test_a_refused_login_is_remembered_and_explained(rig):
    exp, client, _farm, _clock = rig
    client.up(reason="Not authorized")
    exp.tick()
    st = exp.status()
    assert st["state"] == "connecting" and "username or password" in st["error"] and client.sent == []
    client.on_connect_fail(client, None)
    assert "broker.local:1883" in exp.status()["error"]


def test_testing_a_broker_publishes_nothing_and_says_what_happened():
    FakeClient.made = []

    def factory(cfg, will=None):
        c = FakeClient(cfg, will)
        c.connect_async = lambda host, port, keepalive=60: c.up()
        return c

    assert ha.check_broker(MqttExport(host="b", port=1884), factory)["ok"]
    c = FakeClient.made[-1]
    assert c.sent == [] and ("disconnect",) in c.calls
    refused = ha.check_broker(MqttExport(host="b"), lambda cfg, will=None: _refusing(cfg))
    assert not refused["ok"] and "username or password" in refused["detail"]
    silent = ha.check_broker(MqttExport(host="b", tls=True), FakeClient, timeout=0.05)
    assert not silent["ok"] and "b:8883" in silent["detail"] and "TLS" in silent["detail"]
    assert not ha.check_broker(MqttExport())["ok"]


def _refusing(cfg):
    c = FakeClient(cfg)
    c.connect_async = lambda host, port, keepalive=60: c.up("Not authorized")
    return c


def test_the_password_is_not_part_of_what_the_page_gets_back(tmp_path):
    from fastapi.testclient import TestClient

    from proofloop.app.loop_server import create_app
    from proofloop.monitor.service import App

    app = App(tmp_path, connect=False)
    c, phone = TestClient(create_app(app)), TestClient(create_app(app, allow_send=False))
    body = {"enabled": True, "host": "broker.local", "username": "u", "password": "s3cret", "tls": True}
    got = c.put("/api/home-assistant", json=body).json()
    assert got["host"] == "broker.local" and got["has_password"] and "s3cret" not in json.dumps(got)
    assert "s3cret" not in json.dumps(c.get("/api/home-assistant").json())
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["mqtt_export"]["password"] == "s3cret"
    c.put("/api/home-assistant", json={**body, "password": "", "port": 8884})  # empty keeps the saved password
    assert app.settings.mqtt_export.password == "s3cret" and app.settings.mqtt_export.port == 8884
    seen = phone.get("/api/home-assistant").json()
    assert set(seen) == {"enabled", "status"}
    assert phone.put("/api/home-assistant", json=body).status_code == 403
    assert phone.post("/api/home-assistant/test", json=body).status_code == 403
    assert c.put("/api/home-assistant", json={**body, "base_topic": "a/#"}).status_code == 422
    assert c.put("/api/home-assistant", json={**body, "host": ""}).status_code == 422
    # nothing was started: an App made without printers' connections never opens a broker connection
    assert app.homeassistant.status()["state"] == "paused"
