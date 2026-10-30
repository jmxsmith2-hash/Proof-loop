"""A pretend Bambu printer on 127.0.0.1, speaking the same LAN protocol as a real one.

It runs what ProofLoop talks to on a real printer, each on a free port:

- MQTT over TLS (a real printer's 8883): user ``bblp`` with the access code; reports go out on
  ``device/<serial>/report`` and commands come in on ``device/<serial>/request``. Enough of MQTT
  3.1.1 for one printer: connect, subscribe, publish (QoS 0 and 1), ping, disconnect.
- FTPS with TLS from the first byte (a real printer's 990), passive mode, the data connection on
  TLS too: enough to log in and store a file.
- A camera: one JPEG over HTTP, used as the printer's ``snapshot_url``.

Like a printer, it starts a file it holds when told ``project_file``, and pauses and resumes when
told. Tests drive the rest with ``report``. With ``simulate`` (``proofloop try``), it prints the file
itself, faster than real life, by walking its G-code: a layer at each ``; CHANGE_LAYER``, the
FarmBoard steps at each ``M104 S1``-``S5``, and a pause at each ``M400 U1``, where it waits for
ProofLoop to check the plate and resume. Now and then a part stays on the plate; then a pretend
person clears it and presses Resume after a while, as you would.
"""

from __future__ import annotations

import datetime
import io
import json
import random
import re
import socket
import ssl
import struct
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WHITE_PLA = [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "F2F2F2FF", "remain": 80}]}]


def plate_picture(parts_left: bool = False, seed: int = 1) -> bytes:
    """What a camera sees of a textured PEI plate from above, empty or with a part left on it."""
    import numpy as np
    from PIL import Image, ImageDraw

    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:480, 0:640]
    base = 90 + rng.normal(0, 4, (480, 640)) + 25 * np.sin(xx / 37.0) * np.cos(yy / 53.0)
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8)).convert("RGB")
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 640, 70), fill=(30, 30, 30))  # the toolhead, outside the plate area
    if parts_left:
        draw.rectangle((300, 250, 340, 290), fill=(230, 230, 230))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def steps(gcode: str) -> list[tuple[str, int]]:
    """What a printer does while it runs this G-code, in order: ("layers", total), ("layer", n),
    ("signal", 1..6) for the FarmBoard's M104 S1-S6, ("pause", 0) for M400 U1."""
    out, layer = [], 0
    for line in gcode.splitlines():
        if line.startswith("; total layer number:"):
            out.append(("layers", int(line.rsplit(":", 1)[1])))
            layer = 0
        elif line.startswith("; CHANGE_LAYER"):
            layer += 1
            out.append(("layer", layer))
        elif line.startswith("M400 U1"):
            out.append(("pause", 0))
        else:
            m = re.match(r"M104 S([1-6])\b", line)
            if m:
                out.append(("signal", int(m.group(1))))
    return out


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
    def __init__(self, serial: str = "01P00A000000001", access_code: str = "12345678", simulate: bool = False,
                 layer_s: float = 0.25, step_s: float = 4.0, parts_left: float = 0.0, person_s: float = 25.0):
        self.serial, self.access_code = serial, access_code
        self.simulate, self.layer_s, self.step_s = simulate, layer_s, step_s
        self.parts_left, self.person_s = parts_left, person_s  # chance a part stays; how soon you clear it
        self._rng = random.Random(serial)
        self._resumed = threading.Event()
        self._job: threading.Thread | None = None
        self._tmp = tempfile.TemporaryDirectory()
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(*_certificate(Path(self._tmp.name)))
        self.state: dict = {"gcode_state": "IDLE", "layer_num": 0, "total_layer_num": 0, "mc_remaining_time": 0,
                            "subtask_name": "", "gcode_file": "", "print_error": 0, "nozzle_target_temper": 0,
                            "ams": {"ams": WHITE_PLA, "tray_now": "255"}}
        self.files: dict[str, bytes] = {}
        self.commands: list[dict] = []  # every command ProofLoop sent, in order
        self.picture = plate_picture() if simulate else b""  # what the camera shows
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
            if self.simulate:
                self._job = threading.Thread(target=self._print, args=(name, cmd.get("param", "")), daemon=True)
                self._job.start()
        elif what == "pause":
            self.report(gcode_state="PAUSE")
        elif what == "resume":
            self.report(gcode_state="RUNNING")
            self._resumed.set()

    # ---- printing, when simulating --------------------------------------------------------------
    def _print(self, name: str, param: str) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(self.files[name])) as z:
                path = param if param in z.namelist() else next(n for n in z.namelist() if n.endswith(".gcode"))
                gcode = z.read(path).decode("utf-8", "replace")
        except (KeyError, StopIteration, zipfile.BadZipFile):
            self.report(gcode_state="FAILED", print_error=0x0500C010)
            return
        todo = steps(gcode)
        layers = next((n for kind, n in todo if kind == "layers"), 1)
        self.report(ams={"tray_now": "0"}, total_layer_num=layers, bed_temper=65.0, bed_target_temper=65.0,
                    nozzle_temper=220.0)
        for i, (kind, n) in enumerate(todo):
            if self._stop.is_set():
                return
            if kind == "layers":
                layers = n
                self.report(total_layer_num=n, layer_num=0)
            elif kind == "layer":
                left = sum(1 for k, _ in todo[i:] if k == "layer")
                self.report(layer_num=n, mc_remaining_time=max(1, round(left * 60 * self.layer_s / 60)),
                            mc_percent=min(99, round(100 * (len(todo) - left) / len(todo))))
                time.sleep(self.layer_s)
            elif kind == "signal":
                if n == 1:  # a new loop: the bed heats again
                    self.report(nozzle_target_temper=n, bed_temper=65.0, bed_target_temper=65.0, nozzle_temper=220.0)
                elif n == 2:  # print done: the door opens and the bed cools to the release temperature
                    self.report(nozzle_target_temper=n, bed_temper=30.0, bed_target_temper=0.0, nozzle_temper=60.0)
                else:
                    self.report(nozzle_target_temper=n)
                time.sleep(self.step_s)
            elif kind == "pause":
                self._pause()
        self.report(gcode_state="FINISH", mc_remaining_time=0, mc_percent=100, nozzle_target_temper=0,
                    ams={"tray_now": "255"}, bed_temper=28.0, bed_target_temper=0.0, nozzle_temper=35.0)

    def _pause(self) -> None:
        """The file waits here for the plate check: ProofLoop resumes it, or a pretend person does."""
        left = self._rng.random() < self.parts_left
        self.picture = plate_picture(parts_left=left)
        self._resumed.clear()
        self.report(gcode_state="PAUSE", mc_remaining_time=0)
        if not self._resumed.wait(self.person_s if left else self.person_s * 4):
            self.picture = plate_picture()  # you cleared the plate and pressed Resume
            self.report(gcode_state="RUNNING")
        self.picture = plate_picture()

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
