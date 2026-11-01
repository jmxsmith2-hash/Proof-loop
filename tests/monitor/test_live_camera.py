"""The camera as live video: one connection while someone watches, closed again after."""

import io
import time

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import PrinterConfig
from proofloop.monitor.camera_live import LiveCamera, jpeg_frames
from proofloop.monitor.printer.bambu import BambuPrinter
from proofloop.monitor.service import App
from tests.monitor.test_send import FakeLink


def jpeg(n: int) -> bytes:
    return b"\xff\xd8" + bytes([n]) * 50 + b"\xff\x00" + b"\xff\xd9"  # 0xFF in image data is followed by 0x00


def test_frames_are_split_from_a_stream_of_jpegs_whatever_the_chunks():
    stream = io.BytesIO(b"junk" + jpeg(1) + jpeg(2) + jpeg(3)[:20])
    assert list(jpeg_frames(stream, chunk=7)) == [jpeg(1), jpeg(2)]  # the cut-off third never comes out


def test_one_connection_serves_every_viewer_and_closes_when_nobody_watches():
    opened = []

    def frames():
        opened.append(time.monotonic())
        n = 0
        while True:
            n += 1
            time.sleep(0.01)
            yield jpeg(n % 250)

    cam = LiveCamera(frames, idle_s=0.3)
    assert cam.fresh() is None and not cam.running  # nothing opens until someone looks
    a, frame, _ = cam.next_frame(0)
    b, _, _ = cam.next_frame(a)
    assert frame.startswith(b"\xff\xd8") and b > a
    assert cam.fresh() is not None and len(opened) == 1  # the second viewer's frame came from the same camera
    deadline = time.monotonic() + 3
    while cam.running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not cam.running and cam.fresh() is None  # closed by itself once nobody asked


def test_a_camera_that_fails_says_why_and_is_tried_again():
    tries = []

    def broken():
        tries.append(1)
        raise RuntimeError("camera did not answer")

    cam = LiveCamera(broken, idle_s=2)
    seq, frame, error = cam.next_frame(0, timeout=0.5)
    assert (seq, frame, error) == (0, b"", "camera did not answer")
    time.sleep(1.2)
    assert len(tries) >= 2


def test_while_the_view_is_open_a_snapshot_is_its_latest_frame():
    p = BambuPrinter("127.0.0.1", "01", "12345678", "N7")
    p.live = LiveCamera(lambda: iter([jpeg(7)] + [jpeg(8)] * 10_000), idle_s=1)
    p.live.next_frame(0)
    assert p.snapshot() in (jpeg(7), jpeg(8))  # no second camera connection


def test_the_live_endpoint_streams_pictures_from_any_printer(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    app.add_printer(PrinterConfig(name="p2s", host="127.0.0.1", serial="01", access_code="12345678", model_id="N7"))
    pictures = iter(range(1, 100))
    app.watches["p2s"].link.snapshot = lambda timeout=20: jpeg(next(pictures))
    try:
        c = TestClient(create_app(app))
        r = c.get("/api/monitor/p2s/live?frames=3")
        assert r.status_code == 200 and r.headers["content-type"].startswith("multipart/x-mixed-replace")
        assert r.content.count(b"Content-Type: image/jpeg") == 3 and jpeg(1) in r.content
        assert c.get("/api/monitor/nope/live").status_code == 404
    finally:
        app.shutdown()
