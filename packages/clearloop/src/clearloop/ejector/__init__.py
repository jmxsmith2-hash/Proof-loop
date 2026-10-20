"""Hardware-agnostic ejector drivers.

The loop controller only needs four verbs from ejection hardware: get out of the
way before a print (``prepare``), clear the plate (``eject``), open or close the
door, and report status. Anything that can do that over HTTP, MQTT, or purely in
G-code can be driven by ClearLoop: the ClearBoard reference firmware, a FarmLoop
kit in mechanical mode, SwapMod or PrintFlow3D plate changers, a 3DQue-style
frictionless bed, or a DIY pusher on a Home Assistant relay.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from ..gcode.routines import EjectMode


@dataclass
class EjectResult:
    ok: bool
    message: str = ""
    duration_s: float = 0.0
    peak_current_a: float | None = None
    stalled: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


class Ejector(ABC):
    kind: str = "base"
    gcode_mode: EjectMode = "gcode"
    custom_gcode: str = ""

    def prepare(self) -> EjectResult:
        return EjectResult(True, "nothing to prepare")

    @abstractmethod
    def eject(self, profile: str = "standard") -> EjectResult: ...

    def door(self, open_: bool) -> EjectResult:
        return EjectResult(True, "no door opener")

    def status(self) -> dict:
        return {"kind": self.kind}

    def plate_used(self) -> None:  # noqa: B027 - optional hook, most ejectors ignore it
        """Called once per completed loop (plate changers count their magazine)."""


class GcodeOnlyEjector(Ejector):
    """No electronics: tilt + scraper, frictionless beds, DIY pushers. The sweep is the eject."""

    kind = "gcode"
    gcode_mode = "gcode"

    def eject(self, profile: str = "standard") -> EjectResult:
        return EjectResult(True, "printer motion only")


class CustomGcodeEjector(Ejector):
    """Vendor or DIY end-of-print snippet (FarmLoop mechanical trigger, SwapMod, PrintFlow3D...)."""

    kind = "custom"
    gcode_mode = "custom"

    def __init__(self, gcode: str, magazine_size: int | None = None):
        self.custom_gcode = gcode
        self.magazine_size = magazine_size
        self.remaining = magazine_size
        self._lock = threading.Lock()

    def prepare(self) -> EjectResult:
        if self.remaining is not None and self.remaining <= 0:
            return EjectResult(False, "plate magazine is empty; reload plates and reset the counter")
        return EjectResult(True)

    def eject(self, profile: str = "standard") -> EjectResult:
        return EjectResult(True, "handled by the custom G-code snippet")

    def plate_used(self) -> None:
        with self._lock:
            if self.remaining is not None:
                self.remaining -= 1

    def reload(self) -> None:
        self.remaining = self.magazine_size

    def status(self) -> dict:
        return {"kind": self.kind, "magazine_size": self.magazine_size, "remaining": self.remaining}


class HttpEjector(Ejector):
    """ClearBoard firmware, or any device with an HTTP endpoint per action."""

    kind = "http"
    gcode_mode = "board"

    def __init__(
        self,
        base_url: str = "",
        eject_url: str | None = None,
        prepare_url: str | None = None,
        door_url: str | None = None,
        status_url: str | None = None,
        token: str | None = None,
        timeout_s: float = 30.0,
        kind: str = "http",
    ):
        base = base_url.rstrip("/")
        self.kind = kind
        self.eject_url = eject_url or (base + "/api/cycle" if base else None)
        self.prepare_url = prepare_url or (base + "/api/retract" if base else None)
        self.door_url = door_url or (base + "/api/door" if base else None)
        self.status_url = status_url or (base + "/api/status" if base else None)
        self.token = token
        self.timeout_s = timeout_s
        if not self.eject_url:
            raise ValueError("http ejector needs base_url or eject_url")

    def _post(self, url: str | None, body: dict) -> EjectResult:
        if not url:
            return EjectResult(True, "not configured")
        started = time.monotonic()
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                text = r.read().decode() or "{}"
        except urllib.error.HTTPError as e:
            return EjectResult(False, f"HTTP {e.code} from {url}", time.monotonic() - started)
        except (urllib.error.URLError, OSError) as e:
            return EjectResult(False, f"ejector unreachable at {url}: {e}", time.monotonic() - started)
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return EjectResult(
            ok=bool(data.get("ok", True)) and not data.get("stalled", False),
            message=str(data.get("message", "")),
            duration_s=float(data.get("duration_s", time.monotonic() - started)),
            peak_current_a=data.get("peak_current_a"),
            stalled=bool(data.get("stalled", False)),
        )

    def prepare(self) -> EjectResult:
        return self._post(self.prepare_url, {})

    def eject(self, profile: str = "standard") -> EjectResult:
        return self._post(self.eject_url, {"profile": profile})

    def door(self, open_: bool) -> EjectResult:
        return self._post(self.door_url, {"open": open_})

    def status(self) -> dict:
        out = {"kind": self.kind, "url": self.eject_url}
        if self.status_url:
            try:
                with urllib.request.urlopen(self.status_url, timeout=5) as r:
                    out["device"] = json.loads(r.read() or b"{}")
                out["reachable"] = True
            except (urllib.error.URLError, OSError, ValueError):
                out["reachable"] = False
        return out


class MqttEjector(Ejector):
    """Publish a command and wait for an acknowledgement on another topic."""

    kind = "mqtt"
    gcode_mode = "board"

    def __init__(
        self,
        host: str,
        command_topic: str,
        ack_topic: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        timeout_s: float = 30.0,
    ):
        import paho.mqtt.client as mqtt

        self.command_topic = command_topic
        self.ack_topic = ack_topic
        self.timeout_s = timeout_s
        self._acks: dict[str, dict] = {}
        self._cv = threading.Condition()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if username:
            self._client.username_pw_set(username, password)
        self._client.on_connect = lambda c, *a: c.subscribe(ack_topic)
        self._client.on_message = self._on_message
        self._client.connect_async(host, port)
        self._client.loop_start()

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        with self._cv:
            self._acks[str(data.get("id"))] = data
            self._cv.notify_all()

    def _call(self, action: str, **params) -> EjectResult:
        cid = f"{action}-{time.time_ns()}"
        self._client.publish(self.command_topic, json.dumps({"id": cid, "action": action, **params}))
        deadline = time.monotonic() + self.timeout_s
        with self._cv:
            while cid not in self._acks:
                left = deadline - time.monotonic()
                if left <= 0:
                    return EjectResult(
                        False, f"no acknowledgement for {action} within {self.timeout_s:.0f} s"
                    )
                self._cv.wait(left)
            data = self._acks.pop(cid)
        return EjectResult(
            bool(data.get("ok", False)) and not data.get("stalled"),
            str(data.get("message", "")),
            float(data.get("duration_s", 0)),
            data.get("peak_current_a"),
            bool(data.get("stalled")),
        )

    def prepare(self) -> EjectResult:
        return self._call("retract")

    def eject(self, profile: str = "standard") -> EjectResult:
        return self._call("cycle", profile=profile)

    def door(self, open_: bool) -> EjectResult:
        return self._call("door", open=open_)


def build_ejector(cfg) -> Ejector:
    """Create an ejector from an ``EjectorConfig``."""
    kind = cfg.kind
    if kind == "gcode":
        return GcodeOnlyEjector()
    if kind == "custom":
        return CustomGcodeEjector(cfg.gcode or "", cfg.magazine_size)
    if kind in ("clearboard", "http", "webhook"):
        return HttpEjector(
            cfg.url or "",
            cfg.eject_url,
            cfg.prepare_url,
            cfg.door_url,
            cfg.status_url,
            cfg.token,
            cfg.timeout_s,
            kind=kind,
        )
    if kind == "mqtt":
        return MqttEjector(
            cfg.mqtt_host,
            cfg.command_topic,
            cfg.ack_topic,
            cfg.mqtt_port,
            cfg.username,
            cfg.password,
            cfg.timeout_s,
        )
    raise ValueError(f"unknown ejector kind {kind!r}")
