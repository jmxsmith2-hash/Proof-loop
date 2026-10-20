"""LAN-only connection to one Bambu Lab printer.

Requires LAN-only mode with Developer Mode enabled (see docs/bambu-setup.md):
MQTT over TLS on 8883 for status and commands, implicit FTPS on 990 for file
upload, and the chamber camera for bed verification. Nothing here talks to
Bambu Cloud.
"""

from __future__ import annotations

import ftplib
import io
import json
import logging
import secrets
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable

from .state import PrinterState

log = logging.getLogger(__name__)


def _insecure_context() -> ssl.SSLContext:
    # Printers present a self-signed certificate; the LAN access code is the credential.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _ImplicitFTPS(ftplib.FTP_TLS):
    """Implicit-TLS FTP with TLS session reuse on the data channel (the printer insists)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class BambuPrinter:
    """Implements the ``PrinterLink`` interface the loop controller expects."""

    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        camera: str = "tcp_jpeg",
        snapshot_url: str | None = None,
        start_url_template: str = "file:///sdcard/{name}",
        use_ams: bool = True,
    ):
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.camera = camera
        self.snapshot_url = snapshot_url
        self.start_url_template = start_url_template
        self.use_ams = use_ams
        self.state = PrinterState()
        self._listeners: list[Callable[[PrinterState], None]] = []
        self._lock = threading.Lock()
        self._client = None

    # ---- MQTT -------------------------------------------------------------
    def add_listener(self, fn: Callable[[PrinterState], None]) -> None:
        self._listeners.append(fn)

    def connect(self) -> None:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"clearloop-{self.serial}-{secrets.token_hex(3)}"
        )
        client.username_pw_set("bblp", self.access_code)
        client.tls_set_context(_insecure_context())
        client.reconnect_delay_set(1, 30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(self.host, 8883, keepalive=30)
        client.loop_start()
        self._client = client

    def close(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error(
                "%s: MQTT connect refused (%s); check the access code and LAN-only mode",
                self.serial,
                reason_code,
            )
            return
        client.subscribe(f"device/{self.serial}/report")
        self.state.online = True
        self.request({"pushing": {"command": "pushall", "version": 1, "push_target": 1}})
        self.request({"info": {"command": "get_version"}})

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.state.online = False
        self._notify()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        with self._lock:
            self.state.merge(payload)
        self._notify()

    def _notify(self):
        for fn in self._listeners:
            try:
                fn(self.state)
            except Exception:  # a listener must never kill the MQTT thread
                log.exception("printer listener failed")

    def request(self, payload: dict) -> None:
        if not self._client:
            raise RuntimeError("not connected")
        for section in payload.values():
            section.setdefault("sequence_id", str(int(time.time() * 1000) % 1_000_000))
        self._client.publish(f"device/{self.serial}/request", json.dumps(payload))

    # ---- PrinterLink ------------------------------------------------------
    def upload(self, name: str, data: bytes) -> None:
        ftp = _ImplicitFTPS(context=_insecure_context(), timeout=60)
        ftp.connect(self.host, 990)
        try:
            ftp.login("bblp", self.access_code)
            ftp.prot_p()
            ftp.storbinary(f"STOR {name}", io.BytesIO(data))
        finally:
            try:
                ftp.quit()
            except (OSError, ftplib.Error):
                ftp.close()

    def start(self, name: str, plate: int = 1, ams_mapping: list[int] | None = None) -> None:
        self.request(
            {
                "print": {
                    "command": "project_file",
                    "param": f"Metadata/plate_{plate}.gcode",
                    "url": self.start_url_template.format(name=name),
                    "subtask_name": name,
                    "project_id": "0",
                    "profile_id": "0",
                    "task_id": "0",
                    "subtask_id": "0",
                    "md5": "",
                    "timelapse": False,
                    "bed_type": "auto",
                    "bed_levelling": True,
                    "flow_cali": False,
                    "vibration_cali": False,
                    "layer_inspect": False,
                    "use_ams": self.use_ams,
                    "ams_mapping": ams_mapping or [],
                }
            }
        )

    def start_gcode(self, name: str) -> None:
        """Run a plain .gcode file (used for recovery sweeps)."""
        self.request({"print": {"command": "gcode_file", "param": f"/sdcard/{name}"}})

    def pause(self) -> None:
        self.request({"print": {"command": "pause"}})

    def resume(self) -> None:
        self.request({"print": {"command": "resume"}})

    def stop(self) -> None:
        self.request({"print": {"command": "stop"}})

    def gcode_line(self, gcode: str) -> None:
        self.request(
            {"print": {"command": "gcode_line", "param": gcode if gcode.endswith("\n") else gcode + "\n"}}
        )

    def snapshot(self) -> bytes:
        if self.snapshot_url:
            with urllib.request.urlopen(self.snapshot_url, timeout=15) as r:
                return r.read()
        if self.camera == "tcp_jpeg":
            return tcp_jpeg_frame(self.host, self.access_code)
        if self.camera == "rtsps":
            return rtsps_frame(self.host, self.access_code)
        raise RuntimeError("this printer has no camera configured; set snapshot_url for bed verification")


def tcp_jpeg_frame(host: str, access_code: str, timeout: float = 15.0, port: int = 6000) -> bytes:
    """One JPEG from the A1/P1 chamber camera (TLS on port 6000)."""
    auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    auth += b"bblp".ljust(32, b"\0") + access_code.encode().ljust(32, b"\0")
    raw = socket.create_connection((host, port), timeout=timeout)
    with _insecure_context().wrap_socket(raw, server_hostname=host) as s:
        s.sendall(auth)
        header = _recv_exact(s, 16)
        size = struct.unpack("<I", header[:4])[0]
        if not 0 < size < 10_000_000:
            raise RuntimeError(f"unexpected camera frame size {size}")
        frame = _recv_exact(s, size)
    if not (frame.startswith(b"\xff\xd8") and frame.rstrip(b"\0").endswith(b"\xff\xd9")):
        raise RuntimeError("camera returned something that is not a JPEG")
    return frame


def _recv_exact(s, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("camera connection closed")
        buf += chunk
    return bytes(buf)


def rtsps_frame(host: str, access_code: str, timeout: float = 20.0) -> bytes:
    """One JPEG from the X1/H2/P2 RTSPS stream (needs ffmpeg on PATH)."""
    url = f"rtsps://bblp:{access_code}@{host}:322/streaming/live/1"
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        url,
        "-frames:v",
        "1",
        "-f",
        "image2",
        "-",
    ]
    out = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError(f"ffmpeg could not grab a frame: {out.stderr.decode(errors='replace')[:200]}")
    return out.stdout
