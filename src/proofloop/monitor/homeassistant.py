"""Home Assistant / MQTT export: each watched printer's state, published to your own MQTT broker.

Off unless you turn it on (*Monitor › Home Assistant*). Each printer becomes a Home Assistant device through
MQTT discovery: a retained ``config`` message per sensor tells Home Assistant what exists, and one retained
JSON message per printer (``<base>/<printer>/state``) carries the values. ``<base>/status`` is the
availability topic: ``online`` while ProofLoop is connected, ``offline`` when it stops or the broker loses
it (the connection's last will).

This is read-only. ProofLoop publishes and subscribes to nothing, so nothing on the broker can control a
printer. It runs in its own threads (the MQTT client's, and one that looks at the printers about once a
second), reconnects with a growing wait, and a broker problem is only logged: watching printers never
depends on it. A printer's message is sent when it changed, at most once every ``INTERVAL`` seconds.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import ssl
import threading
import time
from collections.abc import Callable
from hashlib import sha1
from typing import Any

from proofloop import __version__
from proofloop.core import printers
from proofloop.core.settings import MqttExport

log = logging.getLogger(__name__)
INTERVAL = 5.0  # seconds between messages for one printer
STATES = {
    "IDLE": "idle",
    "PREPARE": "preparing",
    "RUNNING": "printing",
    "PAUSE": "paused",
    "FINISH": "finished",
    "FAILED": "failed",
}
STATE_OPTIONS = ["idle", "preparing", "printing", "paused", "finished", "failed", "offline", "unknown"]
PLATE_OPTIONS = ["empty", "parts left", "not checked"]


def slug(name: str) -> str:
    """A printer's name as Home Assistant ids may hold it: letters, digits and underscores."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "printer"


def _failed(reason_code: Any) -> bool:
    is_failure = getattr(reason_code, "is_failure", None)
    return bool(is_failure) if is_failure is not None else reason_code != 0


def _why(reason_code: Any) -> str:
    text = str(reason_code).lower()
    if "not authorized" in text or "bad user" in text or "bad username" in text:
        return "the broker refused the username or password"
    return f"the broker refused the connection ({reason_code})"


def make_client(cfg: MqttExport, will: tuple[str, str] | None = None):
    """A paho client for ``cfg`` (not connected yet). ``will``: the topic and payload sent if it drops."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"proofloop-export-{secrets.token_hex(4)}")
    if cfg.username:
        client.username_pw_set(cfg.username, cfg.password)
    if cfg.tls:
        ctx = ssl.create_default_context()
        if not cfg.tls_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        client.tls_set_context(ctx)
    if will:
        client.will_set(will[0], will[1], qos=1, retain=True)
    client.reconnect_delay_set(1, 60)  # wait longer after each failed try, up to a minute
    return client


def check_broker(cfg: MqttExport, client_factory: Callable[..., Any] | None = None, timeout: float = 8.0) -> dict:
    """Log in to the broker and hang up again, publishing and subscribing to nothing: ``{"ok", "detail"}``."""
    done, result = threading.Event(), {"ok": False, "detail": ""}
    host, port = cfg.host, cfg.port_or_default
    if not host:
        return {"ok": False, "detail": "Enter the broker's address first."}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if _failed(reason_code):
            result["detail"] = _why(reason_code)
        else:
            result.update(ok=True, detail=f"Connected to {host}:{port}.")
        done.set()

    def on_connect_fail(client, userdata):
        result["detail"] = (f"Can't reach {host}:{port}. Check the address and port"
                            + (", and that the broker uses TLS here." if cfg.tls else ", and the TLS setting."))
        done.set()

    client = None
    try:
        client = (client_factory or make_client)(cfg)
        client.on_connect = on_connect
        client.on_connect_fail = on_connect_fail
        client.connect_async(host, port, keepalive=15)
        client.loop_start()
        if not done.wait(timeout) and not result["detail"]:
            result["detail"] = (f"No answer from {host}:{port}. Check the address and port"
                                + (", and that the broker uses TLS here." if cfg.tls else ", and the TLS setting."))
    except Exception as e:  # noqa: BLE001 - a test of the broker answers with what went wrong
        log.info("broker check failed", exc_info=True)
        result["detail"] = f"Couldn't connect: {type(e).__name__}."
    finally:
        if client is not None:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception:  # noqa: BLE001 - hanging up is best effort
                log.debug("closing the test connection failed", exc_info=True)
    return result


class HomeAssistantExport:
    """Publishes ``app``'s printers to the broker in ``cfg``. ``configure`` (re)starts or stops it."""

    def __init__(self, app, client_factory: Callable[..., Any] | None = None, active: bool = True,
                 thread: bool = True, interval: float = INTERVAL, clock: Callable[[], float] = time.monotonic):
        self.app = app
        self.client_factory = client_factory or make_client
        self.active = active  # False: never connects (an App made without printers' connections, e.g. in tests)
        self.use_thread = thread
        self.interval = interval
        self.now = clock
        self.cfg = MqttExport()
        self.client: Any = None
        self.connected = False
        self.error: str | None = None
        self.published = 0
        self.last_publish = 0.0
        self._failing = False
        self._resync = True
        self._sent: dict[str, dict] = {}  # printer -> the last state message sent
        self._at: dict[str, float] = {}  # printer -> when
        self._known: dict[str, str] = {}  # printer -> the id its entities were announced under
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    # ---- topics ---------------------------------------------------------------------------
    @property
    def status_topic(self) -> str:
        return f"{self.cfg.base_topic}/status"

    def state_topic(self, sl: str) -> str:
        return f"{self.cfg.base_topic}/{sl}/state"

    def config_topic(self, component: str, sl: str, obj: str) -> str:
        return f"{self.cfg.discovery_prefix}/{component}/proofloop_{sl}/{obj}/config"

    # ---- start and stop -------------------------------------------------------------------
    def configure(self, cfg: MqttExport) -> None:
        """Use these settings: nothing happens if they are the ones already in use."""
        with self._lock:
            if cfg == self.cfg and (self._thread is not None or self.client is not None or not cfg.enabled):
                return
            self._stop_locked()
            self.cfg = cfg.model_copy()
            self.error = None
            if cfg.enabled and cfg.host and self.active:
                self._start_locked()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _start_locked(self) -> None:
        self._stop = threading.Event()
        self.connected, self._resync, self._failing = False, True, False
        self._sent.clear()
        self._at.clear()
        try:
            client = self.client_factory(self.cfg, (self.status_topic, "offline"))
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_connect_fail = self._on_connect_fail
            client.connect_async(self.cfg.host, self.cfg.port_or_default, keepalive=30)
            client.loop_start()
        except Exception as e:  # noqa: BLE001 - a broker problem must never reach the printers
            log.warning("Home Assistant export: couldn't start (%s)", type(e).__name__, exc_info=True)
            self.error = f"couldn't start: {type(e).__name__}"
            return
        self.client = client
        if self.use_thread:
            self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True,
                                            name="proofloop-mqtt-export")
            self._thread.start()

    def _stop_locked(self) -> None:
        self._stop.set()
        thread, client = self._thread, self.client
        self._thread, self.client = None, None
        if thread is not None:
            thread.join(timeout=5)
        if client is not None:
            try:
                if self.connected:  # a clean goodbye: the last will only fires when the connection is lost
                    client.publish(self.status_topic, "offline", qos=1, retain=True).wait_for_publish(2)
                client.disconnect()
                client.loop_stop()
            except Exception:  # noqa: BLE001 - hanging up is best effort
                log.debug("Home Assistant export: closing failed", exc_info=True)
        self.connected = False

    # ---- the client's callbacks (its own thread) ------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if _failed(reason_code):
            self.error = _why(reason_code)
            log.warning("Home Assistant export: %s", self.error)
            return
        log.info("Home Assistant export: connected to %s", self.cfg.host)
        self.connected, self.error, self._failing, self._resync = True, None, False, True

    def _on_disconnect(self, client, userdata, flags, reason_code=None, properties=None) -> None:
        if self.connected:
            log.warning("Home Assistant export: lost the connection to the broker; trying again")
        self.connected = False

    def _on_connect_fail(self, client, userdata) -> None:
        self.error = f"can't reach the broker at {self.cfg.host}:{self.cfg.port_or_default}"
        if not self._failing:  # one line per run of failures: the client keeps trying, waiting longer each time
            log.warning("Home Assistant export: %s; will keep trying", self.error)
        self._failing = True

    # ---- publishing -----------------------------------------------------------------------------
    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(1.0):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the export must never stop or hurt anything else
                log.exception("Home Assistant export: publishing failed")

    def tick(self) -> None:
        """Send what changed. Called about once a second; does nothing while the broker isn't connected."""
        if not self.connected or self.client is None:
            return
        now = self.now()
        if self._resync:
            if not self._publish(self.status_topic, "online"):
                return
            self._known.clear()
            self._sent.clear()
            self._at.clear()
            self._resync = False
        watches = dict(self.app.watches)
        slugs = self._slugs(list(watches))
        for name in [n for n in self._known if n not in watches]:  # a printer you removed: take its sensors away
            self._forget(name)
        for name, w in watches.items():
            sl = slugs[name]
            try:
                if self._known.get(name) != sl:
                    if not self._announce(name, sl):
                        continue
                    self._known[name] = sl
                state = self.snapshot(name, w)
            except Exception:  # noqa: BLE001 - one printer's problem must not stop the others
                log.exception("Home Assistant export: %s: couldn't read its state", name)
                continue
            if state == self._sent.get(name) or now - self._at.get(name, -1e9) < self.interval:
                continue
            if self._publish(self.state_topic(sl), json.dumps(state)):
                self._sent[name], self._at[name] = state, now

    def _publish(self, topic: str, payload: str | bytes) -> bool:
        try:
            info = self.client.publish(topic, payload, qos=1, retain=True)
            if getattr(info, "rc", 0) != 0:
                return False
        except Exception:  # noqa: BLE001 - a broker problem is only logged
            log.warning("Home Assistant export: couldn't publish", exc_info=True)
            return False
        self.published += 1
        self.last_publish = time.time()
        return True

    @staticmethod
    def _slugs(names: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for n in names:
            s = slug(n)
            if s in out.values():  # two names that look the same to Home Assistant: tell them apart
                s = f"{s}_{sha1(n.encode()).hexdigest()[:4]}"
            out[n] = s
        return out

    def _announce(self, name: str, sl: str) -> bool:
        ok = True
        for topic, payload in self.discovery(name, sl):
            ok = self._publish(topic, json.dumps(payload)) and ok
        return ok

    def _forget(self, name: str) -> None:
        sl = self._known.pop(name)
        for topic, _ in self.discovery(name, sl):
            self._publish(topic, b"")  # an empty retained message removes a discovered entity
        self._publish(self.state_topic(sl), b"")
        self._sent.pop(name, None)
        self._at.pop(name, None)

    def snapshot(self, name: str, w) -> dict:
        """The values published for one printer."""
        s = w.link.state
        job = None
        if w.loop_no:
            _key, job = self.app.tracker.job_for(s)
        check = w.last_check
        return {
            "state": STATES.get(s.gcode_state, "unknown") if s.online else "offline",
            "progress": s.percent,
            "layer": s.layer_num,
            "total_layers": s.total_layers,
            "remaining_min": s.remaining_min,
            "nozzle_temp": round(s.nozzle_temp, 1),
            "bed_temp": round(s.bed_temp, 1),
            "job": (s.subtask_name or "")[:200],
            "plate_check": "not checked" if check is None else "empty" if check.clear else "parts left",
            "loop": w.loop_no,
            "loops_total": job["loops"] if job else None,
            "needs_you": bool(w.waiting_for_you),
        }

    def discovery(self, name: str, sl: str) -> list[tuple[str, dict]]:
        """Home Assistant's discovery messages for one printer: ``(topic, config)``."""
        model_id = next((p.model_id for p in self.app.settings.printers if p.name == name), "")
        device = {
            "identifiers": [f"proofloop_{sl}"],
            "name": name,
            "manufacturer": "Bambu Lab",
            "model": printers.model_name(model_id) if model_id in printers.MODEL_IDS else "3D printer",
            "sw_version": f"ProofLoop {__version__}",
        }
        base = {
            "state_topic": self.state_topic(sl),
            "availability_topic": self.status_topic,
            "has_entity_name": True,
            "device": device,
        }

        def sensor(obj: str, label: str, template: str, **extra: Any) -> tuple[str, dict]:
            cfg = {**base, "name": label, "unique_id": f"proofloop_{sl}_{obj}",
                   "value_template": "{{ value_json." + template + " }}", **extra}
            return self.config_topic("sensor", sl, obj), cfg

        return [
            sensor("state", "State", "state", device_class="enum", options=STATE_OPTIONS, icon="mdi:printer-3d"),
            sensor("progress", "Progress", "progress", unit_of_measurement="%", state_class="measurement",
                   icon="mdi:progress-clock"),
            sensor("layer", "Layer", "layer", state_class="measurement", icon="mdi:layers",
                   json_attributes_topic=self.state_topic(sl),
                   json_attributes_template="{{ {'total_layers': value_json.total_layers} | tojson }}"),
            sensor("remaining", "Time left", "remaining_min", device_class="duration", unit_of_measurement="min",
                   state_class="measurement"),
            sensor("nozzle_temp", "Nozzle temperature", "nozzle_temp", device_class="temperature",
                   unit_of_measurement="°C", state_class="measurement"),
            sensor("bed_temp", "Bed temperature", "bed_temp", device_class="temperature",
                   unit_of_measurement="°C", state_class="measurement"),
            sensor("job", "Job", "job", icon="mdi:file-cog"),
            sensor("plate_check", "Plate check", "plate_check", device_class="enum", options=PLATE_OPTIONS,
                   icon="mdi:camera-iris"),
            sensor("loop", "Loop", "loop", state_class="measurement", icon="mdi:repeat"),
            sensor("loops_total", "Loops in file", "loops_total", state_class="measurement", icon="mdi:repeat"),
            (
                self.config_topic("binary_sensor", sl, "needs_you"),
                {**base, "name": "Needs you", "unique_id": f"proofloop_{sl}_needs_you",
                 "value_template": "{{ 'ON' if value_json.needs_you else 'OFF' }}",
                 "payload_on": "ON", "payload_off": "OFF", "device_class": "problem", "icon": "mdi:hand-back-left"},
            ),
        ]

    # ---- for the page ---------------------------------------------------------------------------
    def status(self) -> dict:
        if not self.cfg.enabled:
            state = "off"
        elif not self.active:
            state = "paused"
        elif self.connected:
            state = "connected"
        else:
            state = "connecting"
        return {
            "state": state,
            "error": None if self.connected else self.error,
            "published": self.published,
            "last_publish": self.last_publish or None,
            "printers": len(self._known),
        }
