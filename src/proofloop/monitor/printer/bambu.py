"""Read-mostly LAN connection to one Bambu Lab printer, for ProofLoop's printer monitor.

The monitor reads status over MQTT, grabs camera frames, and sends ``resume`` (or ``pause``) when a
loop's plate check is done. It uploads and starts a file only through ``send``, which runs only
when you've turned on sending to printers and confirmed that file for that printer (see
``proofloop.monitor.send``). Payloads and ports match Bambuddy, a widely used open-source
Bambu controller (see docs/sources.md):

* MQTT over TLS on 8883, user ``bblp``, password = LAN access code; reports on
  ``device/<serial>/report``, commands on ``device/<serial>/request``.
* ``{"print": {"command": "pause"|"resume", "sequence_id": "0"}}``.
* Camera: A1, A1 mini, P1P, P1S use a JPEG stream over TLS on port 6000; X1,
  X2D, H2 and P2S use RTSPS on port 322, read through a local TLS proxy because
  Debian's GnuTLS-based ffmpeg drops P2S streams.

Control commands need LAN-only mode with Developer Mode on.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import urllib.request
from collections.abc import Callable
from typing import Any

from proofloop.monitor.printer.state import PrinterState

log = logging.getLogger(__name__)

# Model ids (slice_info / MQTT) that use the port-6000 JPEG camera protocol, per Bambuddy.
CHAMBER_IMAGE_MODELS = {"C11", "C12", "N1", "N2S"}


def _insecure_context() -> ssl.SSLContext:
    # Printers present a self-signed certificate; the LAN access code is the credential.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class BambuPrinter:
    mqtt_port, ftp_port = 8883, 990  # the printer's own; tests talk to a pretend printer on others

    def __init__(
        self, host: str, serial: str, access_code: str, model_id: str, snapshot_url: str | None = None
    ):
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.model_id = model_id
        self.snapshot_url = snapshot_url
        self.state = PrinterState()
        self._listeners: list[Callable[[PrinterState], None]] = []
        self._lock = threading.Lock()
        self._client: Any = None

    def add_listener(self, fn: Callable[[PrinterState], None]) -> None:
        self._listeners.append(fn)

    def connect(self) -> None:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"proofloop-{secrets.token_hex(4)}")
        client.username_pw_set("bblp", self.access_code)
        client.tls_set_context(_insecure_context())
        client.reconnect_delay_set(1, 30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(self.host, self.mqtt_port, keepalive=30)
        client.loop_start()
        self._client = client

    def close(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error(
                "%s: MQTT connection refused (%s); check the access code and LAN-only mode",
                self.host,
                reason_code,
            )
            return
        client.subscribe(f"device/{self.serial}/report")
        self.state.online = True
        self._publish({"pushing": {"command": "pushall"}})
        self._publish({"info": {"command": "get_version", "sequence_id": "0"}})
        self._notify()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.state.online = False
        self._notify()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        if not isinstance(payload, dict):
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

    def _publish(self, payload: dict) -> bool:
        if not self._client:
            return False
        self._client.publish(f"device/{self.serial}/request", json.dumps(payload), qos=1)
        return True

    def resume(self) -> bool:
        return self._publish({"print": {"command": "resume", "sequence_id": "0"}})

    def pause(self) -> bool:
        return self._publish({"print": {"command": "pause", "sequence_id": "0"}})

    def send(self, filename: str, data: bytes, command: dict) -> None:
        """Copy the file to the printer's storage, then start it. Raises SendError."""
        from proofloop.monitor.send import SendError, upload

        upload(self.host, self.access_code, filename, data, port=self.ftp_port)
        if not self._publish(command):
            raise SendError("the file is on the printer, but ProofLoop couldn't tell it to start: not connected")

    def snapshot(self, timeout: float = 20.0) -> bytes:
        if self.snapshot_url:
            with urllib.request.urlopen(self.snapshot_url, timeout=timeout) as r:
                return r.read()
        if self.model_id in CHAMBER_IMAGE_MODELS:
            return chamber_image_frame(self.host, self.access_code, timeout)
        return rtsps_frame(self.host, self.access_code, timeout)


def chamber_image_frame(host: str, access_code: str, timeout: float = 15.0, port: int = 6000) -> bytes:
    """One JPEG from the A1/P1 camera: 80-byte auth packet, then 16-byte header + JPEG per frame."""
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
    if not frame.startswith(b"\xff\xd8"):
        raise RuntimeError("the camera returned something that is not a JPEG")
    return frame


def _recv_exact(s, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("camera connection closed")
        buf += chunk
    return bytes(buf)


def rewrite_request_line(data: bytes, proxy_url: bytes, real_url: bytes) -> bytes:
    """Rewrite only the RTSP request line, so Digest auth headers (which embed the URL) stay valid."""
    marker = b" RTSP/1.0"
    if marker not in data:
        return data
    lines = data.split(b"\r\n")
    for i, line in enumerate(lines):
        if line.endswith(marker):
            lines[i] = line.replace(proxy_url, real_url)
            break
    return b"\r\n".join(lines)


class _TlsProxy:
    """Local plain-TCP port that forwards to the printer's RTSPS port using Python's OpenSSL."""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.server = socket.create_server(("127.0.0.1", 0))
        self.local_port = self.server.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self.server.settimeout(0.5)
        while not self._stop.is_set():
            try:
                client, _ = self.server.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket):
        try:
            raw = socket.create_connection((self.host, self.port), timeout=10)
            upstream = _insecure_context().wrap_socket(raw, server_hostname=self.host)
        except OSError as e:
            log.debug("camera proxy: %s", e)
            client.close()
            return
        proxy_url = f"rtsp://127.0.0.1:{self.local_port}".encode()
        real_url = f"rtsps://{self.host}:{self.port}".encode()

        def pump(src, dst, rewrite):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(rewrite_request_line(data, proxy_url, real_url) if rewrite else data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.close()
                    except OSError:
                        pass

        threading.Thread(target=pump, args=(upstream, client, False), daemon=True).start()
        pump(client, upstream, True)

    def close(self):
        self._stop.set()
        self.server.close()


def rtsps_frame(host: str, access_code: str, timeout: float = 20.0, port: int = 322) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is needed for this printer's camera; install ffmpeg")
    proxy = _TlsProxy(host, port)
    url = f"rtsp://bblp:{access_code}@127.0.0.1:{proxy.local_port}/streaming/live/1"
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-rtsp_flags",
        "prefer_tcp",
        "-i",
        url,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-q:v",
        "2",
        "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"camera did not deliver a frame within {timeout:.0f} s") from e
    finally:
        proxy.close()
    if out.returncode != 0 or len(out.stdout) < 100:
        msg = out.stderr.decode(errors="replace").replace(access_code, "********")[:300]
        raise RuntimeError(f"camera frame capture failed: {msg or 'no output from ffmpeg'}")
    return out.stdout
