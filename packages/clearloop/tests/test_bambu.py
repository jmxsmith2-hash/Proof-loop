import json
import shutil
import socket
import ssl
import struct
import subprocess
import threading
from types import SimpleNamespace

import pytest

from clearloop.printer.bambu import BambuPrinter, tcp_jpeg_frame

JPEG = b"\xff\xd8" + b"\x00fakejpegdata" * 20 + b"\xff\xd9"


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl to make a test certificate")
def test_tcp_jpeg_camera_protocol(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "1",
            "-subj",
            "/CN=printer",
        ],
        check=True,
        capture_output=True,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    seen = {}

    def serve():
        conn, _ = srv.accept()
        with ctx.wrap_socket(conn, server_side=True) as s:
            auth = b""
            while len(auth) < 80:
                auth += s.recv(80 - len(auth))
            seen["auth"] = auth
            s.sendall(struct.pack("<IIII", len(JPEG), 0, 1, 0) + JPEG)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    frame = tcp_jpeg_frame("127.0.0.1", "12345678", timeout=5, port=port)
    t.join(5)
    srv.close()
    assert frame == JPEG
    assert seen["auth"][:16] == struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    assert seen["auth"][16:20] == b"bblp" and seen["auth"][48:56] == b"12345678"


def test_mqtt_messages_merge_and_commands_are_shaped():
    p = BambuPrinter("10.0.0.2", "SERIAL", "code")
    got = []
    p.add_listener(lambda s: got.append(s.gcode_state))
    p._on_message(
        None, None, SimpleNamespace(payload=json.dumps({"print": {"gcode_state": "RUNNING"}}).encode())
    )
    p._on_message(None, None, SimpleNamespace(payload=b"not json"))
    assert got == ["RUNNING"]

    sent = []
    p._client = SimpleNamespace(publish=lambda topic, payload: sent.append((topic, json.loads(payload))))
    p.start("cl-1.gcode.3mf", plate=2)
    p.gcode_line("M73 L9004")
    topic, msg = sent[0]
    assert topic == "device/SERIAL/request"
    assert msg["print"]["command"] == "project_file"
    assert msg["print"]["param"] == "Metadata/plate_2.gcode"
    assert msg["print"]["url"] == "file:///sdcard/cl-1.gcode.3mf"
    assert sent[1][1]["print"] == {
        "command": "gcode_line",
        "param": "M73 L9004\n",
        "sequence_id": sent[1][1]["print"]["sequence_id"],
    }
