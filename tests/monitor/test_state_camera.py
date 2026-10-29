import json
import shutil
import socket
import ssl
import struct
import subprocess
import threading
from types import SimpleNamespace

import pytest

from proofloop.monitor.printer.bambu import BambuPrinter, chamber_image_frame, rewrite_request_line
from proofloop.monitor.printer.state import PrinterState

JPEG = b"\xff\xd8" + b"\x00data" * 40 + b"\xff\xd9"


def test_state_merge_and_hms_levels():
    s = PrinterState()
    s.merge(
        {
            "print": {
                "gcode_state": "RUNNING",
                "nozzle_target_temper": 2,
                "layer_num": 3,
                "total_layer_num": 9,
                "hms": [{"attr": 0x03000100, "code": 0x00030008}, {"attr": 1, "code": 5}],
            }
        }
    )
    s.merge({"print": {"layer_num": 4}})
    assert (s.gcode_state, s.nozzle_target, s.layer_num, s.total_layers) == ("RUNNING", 2.0, 4, 9)
    assert len(s.hms) == 1 and s.hms[0].code_str == "0300-0100-0003-0008" and s.hms[0].level == "notification"


def test_rtsp_request_line_rewrite_keeps_auth_header():
    data = b'DESCRIBE rtsp://127.0.0.1:5555/streaming/live/1 RTSP/1.0\r\nAuthorization: Digest uri="rtsp://127.0.0.1:5555/x"\r\n\r\n'
    out = rewrite_request_line(data, b"rtsp://127.0.0.1:5555", b"rtsps://10.0.0.2:322")
    assert out.startswith(b"DESCRIBE rtsps://10.0.0.2:322/streaming/live/1 RTSP/1.0")
    assert b'uri="rtsp://127.0.0.1:5555/x"' in out


def test_commands_are_shaped_like_bambuddy():
    p = BambuPrinter("10.0.0.2", "SER", "code", "N7")
    sent = []
    p._client = SimpleNamespace(publish=lambda t, m, qos=0: sent.append((t, json.loads(m))))
    p.resume()
    p.pause()
    assert sent == [
        ("device/SER/request", {"print": {"command": "resume", "sequence_id": "0"}}),
        ("device/SER/request", {"print": {"command": "pause", "sequence_id": "0"}}),
    ]


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_chamber_image_protocol(tmp_path):
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
    assert chamber_image_frame("127.0.0.1", "12345678", 5, srv.getsockname()[1]) == JPEG
    t.join(5)
    srv.close()
    assert (
        seen["auth"][:16] == struct.pack("<IIII", 0x40, 0x3000, 0, 0) and seen["auth"][48:56] == b"12345678"
    )
