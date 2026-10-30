"""A pretend Bambu printer on 127.0.0.1, speaking the same LAN protocol as a real one.

It runs what ProofLoop talks to on a real printer, each on a free port:

- MQTT over TLS (a real printer's 8883): user ``bblp`` with the access code; reports go out on
  ``device/<serial>/report`` and commands come in on ``device/<serial>/request``. Enough of MQTT
  3.1.1 for one printer: connect, subscribe, publish (QoS 0 and 1), ping, disconnect.
- FTPS with TLS from the first byte (a real printer's 990), passive mode, the data connection on
  TLS too: enough to log in and store a file.
- A camera: one JPEG over HTTP, used as the printer's ``snapshot_url``.

Like a printer, it starts a file it holds when told ``project_file``, and pauses and resumes when
told. Everything else (layers, loop-end pauses, finishing, failing) the test reports with ``report``.
"""

from __future__ import annotations

import datetime
import json
import socket
import ssl
import struct
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WHITE_PLA = [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "F2F2F2FF", "remain": 80}]}]


def _certificate(folder: Path) -> tuple[Path, Path]:
    """A self-signed certificate, as a printer has."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pretend-printer")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2)).sign(key, hashes.SHA256()))
    crt, pem = folder / "printer.crt", folder / "printer.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    return crt, pem


def _server() -> socket.socket:
    s = socket.create_server(("127.0.0.1", 0))
    s.settimeout(0.5)
    return s


class FakePrinter:
    def __init__(self, serial: str = "01P00A000000001", access_code: str = "12345678"):
        self.serial, self.access_code = serial, access_code
        self._tmp = tempfile.TemporaryDirectory()
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(*_certificate(Path(self._tmp.name)))
        self.state: dict = {"gcode_state": "IDLE", "layer_num": 0, "total_layer_num": 0, "mc_remaining_time": 0,
                            "subtask_name": "", "gcode_file": "", "print_error": 0, "nozzle_target_temper": 0,
                            "ams": {"ams": WHITE_PLA, "tray_now": "255"}}
        self.files: dict[str, bytes] = {}
        self.commands: list[dict] = []  # every command ProofLoop sent, in order
        self.picture = b""  # what the camera shows
        self.logins: list[tuple[str, str]] = []
        self._clients: list[ssl.SSLSocket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._mqtt, self._ftp = _server(), _server()
        self.mqtt_port, self.ftp_port = self._mqtt.getsockname()[1], self._ftp.getsockname()[1]
        printer = self

        class Camera(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(printer.picture)

            def log_message(self, *a):
                pass

        self._camera = ThreadingHTTPServer(("127.0.0.1", 0), Camera)
        self.snapshot_url = f"http://127.0.0.1:{self._camera.server_address[1]}/snapshot.jpg"
        for target in (self._accept_mqtt, self._accept_ftp, self._camera.serve_forever):
            threading.Thread(target=target, daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self._camera.shutdown()
        self._camera.server_close()
        for s in (self._mqtt, self._ftp, *self._clients):
            try:
                s.close()
            except OSError:
                pass
        self._tmp.cleanup()

    # ---- what the test does ------------------------------------------------------------
    def report(self, **fields) -> None:
        """Change the printer's state and report it, as a printer does."""
        with self._lock:
            self.state.update(fields)
        self._publish({"print": dict(fields)})

    def connected(self) -> int:
        return len(self._clients)

    def wait_for(self, what, timeout: float = 10.0):
        """Wait until ``what()`` is truthy; return it."""
        end = time.time() + timeout
        while time.time() < end:
            got = what()
            if got:
                return got
            time.sleep(0.02)
        raise AssertionError("the pretend printer waited in vain")

    def command(self, name: str) -> dict | None:
        return next((c for c in self.commands if c.get("command") == name), None)

    # ---- MQTT ----------------------------------------------------------------------------
    def _accept_mqtt(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._mqtt.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._mqtt_client, args=(raw,), daemon=True).start()

    def _mqtt_client(self, raw: socket.socket) -> None:
        raw.settimeout(None)
        try:
            conn = self.tls.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        try:
            while not self._stop.is_set():
                kind, flags, body = _read_packet(conn)
                if kind == 1:  # CONNECT
                    user, password = _credentials(body)
                    ok = user == "bblp" and password == self.access_code
                    conn.sendall(bytes([0x20, 2, 0, 0 if ok else 5]))
                    if not ok:
                        return
                elif kind == 8:  # SUBSCRIBE
                    pid, topics = body[:2], _subscriptions(body[2:])
                    conn.sendall(bytes([0x90, 2 + len(topics)]) + pid + bytes(len(topics)))
                    if f"device/{self.serial}/report" in topics:
                        with self._lock:
                            self._clients.append(conn)
                elif kind == 3:  # PUBLISH
                    qos = (flags >> 1) & 3
                    n = struct.unpack(">H", body[:2])[0]
                    topic, rest = body[2:2 + n].decode(), body[2 + n:]
                    if qos:
                        conn.sendall(bytes([0x40, 2]) + rest[:2])
                        rest = rest[2:]
                    if topic == f"device/{self.serial}/request":
                        self._request(json.loads(rest))
                elif kind == 12:  # PINGREQ
                    conn.sendall(bytes([0xD0, 0]))
                elif kind == 14:  # DISCONNECT
                    return
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            conn.close()

    def _publish(self, payload: dict) -> None:
        topic = f"device/{self.serial}/report".encode()
        body = struct.pack(">H", len(topic)) + topic + json.dumps(payload).encode()
        packet = bytes([0x30]) + _length(len(body)) + body
        with self._lock:
            for c in list(self._clients):
                try:
                    c.sendall(packet)
                except OSError:
                    self._clients.remove(c)

    def _request(self, payload: dict) -> None:
        if "pushing" in payload:  # ProofLoop asks for everything when it connects
            with self._lock:
                state = json.loads(json.dumps(self.state))
            self._publish({"print": state})
            return
        cmd = payload.get("print")
        if not isinstance(cmd, dict):
            return
        self.commands.append(cmd)
        what = cmd.get("command")
        if what == "project_file":
            name = cmd.get("file", "")
            if name not in self.files:
                self.report(gcode_state="FAILED", print_error=0x0500C010)
                return
            self.report(gcode_state="RUNNING", subtask_name=cmd.get("subtask_name", ""), gcode_file=name,
                        layer_num=1, print_error=0)
        elif what == "pause":
            self.report(gcode_state="PAUSE")
        elif what == "resume":
            self.report(gcode_state="RUNNING")

    # ---- FTPS ------------------------------------------------------------------------------
    def _accept_ftp(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._ftp.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._ftp_client, args=(raw,), daemon=True).start()

    def _ftp_client(self, raw: socket.socket) -> None:
        raw.settimeout(10)
        try:
            conn = self.tls.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        f = conn.makefile("rb")
        say = lambda line: conn.sendall(f"{line}\r\n".encode())  # noqa: E731
        user, logged_in, passive = "", False, None
        try:
            say("220 pretend printer ready")
            for line in f:
                verb, _, arg = line.decode().strip().partition(" ")
                verb = verb.upper()
                if verb == "USER":
                    user = arg
                    say("331 password please")
                elif verb == "PASS":
                    self.logins.append((user, arg))
                    logged_in = user == "bblp" and arg == self.access_code
                    say("230 logged in" if logged_in else "530 wrong access code")
                elif not logged_in:
                    say("530 log in first")
                elif verb in ("PBSZ", "PROT", "TYPE"):
                    say("200 ok")
                elif verb == "PASV":
                    passive = _server()
                    port = passive.getsockname()[1]
                    say(f"227 Entering Passive Mode (127,0,0,1,{port >> 8},{port & 0xFF})")
                elif verb == "STOR" and passive is not None:
                    say("150 send it")
                    data_raw, _ = passive.accept()
                    data_raw.settimeout(10)
                    data = self.tls.wrap_socket(data_raw, server_side=True)
                    chunks = []
                    while chunk := data.recv(65536):
                        chunks.append(chunk)
                    try:
                        data.unwrap()  # answer the client's TLS goodbye, as an FTP server does
                    except (OSError, ssl.SSLError):
                        pass
                    data.close()
                    passive.close()
                    passive = None
                    self.files[arg] = b"".join(chunks)
                    say("226 stored")
                elif verb == "QUIT":
                    say("221 bye")
                    return
                else:
                    say("502 not here")
        except (OSError, ssl.SSLError):
            pass
        finally:
            conn.close()


def _read_exact(conn, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _read_packet(conn) -> tuple[int, int, bytes]:
    first = _read_exact(conn, 1)[0]
    length, shift = 0, 0
    while True:
        b = _read_exact(conn, 1)[0]
        length |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    return first >> 4, first & 0x0F, _read_exact(conn, length) if length else b""


def _length(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _strings(data: bytes, at: int) -> tuple[str, int]:
    n = struct.unpack(">H", data[at:at + 2])[0]
    return data[at + 2:at + 2 + n].decode(), at + 2 + n


def _credentials(body: bytes) -> tuple[str | None, str | None]:
    _, at = _strings(body, 0)  # "MQTT"
    flags = body[at + 1]
    _, at = _strings(body, at + 4)  # client id
    if flags & 0x04:  # a will: topic and message
        _, at = _strings(body, at)
        _, at = _strings(body, at)
    user = password = None
    if flags & 0x80:
        user, at = _strings(body, at)
    if flags & 0x40:
        password, at = _strings(body, at)
    return user, password


def _subscriptions(data: bytes) -> list[str]:
    topics, at = [], 0
    while at < len(data):
        topic, at = _strings(data, at)
        topics.append(topic)
        at += 1  # requested QoS
    return topics
