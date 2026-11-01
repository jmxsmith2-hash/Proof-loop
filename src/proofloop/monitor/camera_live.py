"""A printer's camera as live video, while someone is watching it.

Asking for one picture at a time (``snapshot``) means a new camera connection, and on a P2S or X1
waiting for the next keyframe, every time: a picture every second or two. While a camera view is
open, one connection stays up instead and every frame it sends is passed on (about 15 a second
from a P2S). It closes by itself ``IDLE_S`` seconds after the last viewer stops asking, and while
it runs, snapshots (the plate checks and the failure watch) use its frames rather than opening a
second connection to the camera.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import IO

from proofloop.core.logging import get_logger

log = get_logger(__name__)

IDLE_S = 8.0  # keep the camera open this long after the last viewer, for a quick switch back
FRESH_S = 2.0  # a live frame this young stands in for a snapshot
SOI, EOI = b"\xff\xd8", b"\xff\xd9"


def jpeg_frames(stream: IO[bytes], chunk: int = 65536) -> Iterator[bytes]:
    """The JPEGs in a stream of them back to back, as ffmpeg's ``image2pipe`` writes them.

    Inside a JPEG every 0xFF byte of image data is followed by 0x00, so 0xFFD9 only ever marks
    the end of an image.
    """
    buf = bytearray()
    while data := stream.read1(chunk) if hasattr(stream, "read1") else stream.read(chunk):
        buf += data
        while True:
            start = buf.find(SOI)
            if start < 0:
                buf.clear()
                break
            end = buf.find(EOI, start + 2)
            if end < 0:
                del buf[:start]
                break
            yield bytes(buf[start:end + 2])
            del buf[:end + 2]


def polled(snapshot: Callable[[], bytes], every: float = 0.5) -> Iterator[bytes]:
    """Frames for a camera that only gives one picture at a time: as fast as it answers, at most every ``every`` s."""
    while True:
        started = time.monotonic()
        yield snapshot()
        time.sleep(max(0.0, every - (time.monotonic() - started)))


class LiveCamera:
    """One camera connection shared by every viewer, open only while someone is watching."""

    def __init__(self, open_frames: Callable[[], Iterable[bytes]], idle_s: float = IDLE_S):
        self._open = open_frames
        self.idle_s = idle_s
        self._cond = threading.Condition()
        self._frame, self._seq, self._at, self._error = b"", 0, 0.0, ""
        self._wanted_until = 0.0
        self._thread: threading.Thread | None = None
        self.started = 0  # how many times the camera was opened (for tests and the log)

    def want(self) -> None:
        """Someone is watching: keep the camera open (or open it) for another ``idle_s`` seconds."""
        with self._cond:
            self._wanted_until = time.monotonic() + self.idle_s
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="live-camera", daemon=True)
                self._thread.start()

    def next_frame(self, after: int, timeout: float = 5.0) -> tuple[int, bytes, str]:
        """The first frame newer than number ``after``: (its number, the JPEG, the last error).

        Gives back ``after`` unchanged (and no picture) if none came within ``timeout``.
        """
        self.want()
        end = time.monotonic() + timeout
        with self._cond:
            while self._seq <= after and (left := end - time.monotonic()) > 0:
                self._cond.wait(min(left, 1.0))
            if self._seq <= after:
                return after, b"", self._error
            return self._seq, self._frame, self._error

    def fresh(self, max_age: float = FRESH_S) -> bytes | None:
        """The latest frame if the camera is open and it is at most ``max_age`` seconds old."""
        with self._cond:
            if self._thread is not None and self._frame and time.monotonic() - self._at <= max_age:
                return self._frame
        return None

    @property
    def running(self) -> bool:
        with self._cond:
            return self._thread is not None

    def _publish(self, jpeg: bytes) -> None:
        with self._cond:
            self._frame, self._at, self._error = jpeg, time.monotonic(), ""
            self._seq += 1
            self._cond.notify_all()

    def _fail(self, error: str) -> None:
        with self._cond:
            self._error = error
            self._cond.notify_all()

    def _wanted(self) -> bool:
        with self._cond:
            if time.monotonic() < self._wanted_until:
                return True
            self._thread = None  # decided under the lock, so a new viewer starts a new thread
            return False

    def _run(self) -> None:
        backoff = 1.0
        while self._wanted():
            self.started += 1
            frames: Iterable[bytes] = ()
            try:
                frames = self._open()
                for jpeg in frames:
                    self._publish(jpeg)
                    backoff = 1.0
                    if time.monotonic() >= self._wanted_until:
                        break
                else:
                    self._fail("the camera stopped sending pictures")
            except (OSError, RuntimeError, ValueError) as e:  # ValueError: a closed pipe
                log.debug("live camera: %s", e)
                self._fail(str(e)[:200])
            finally:
                close = getattr(frames, "close", None)
                if close:
                    close()
            if time.monotonic() < self._wanted_until:
                time.sleep(backoff)  # connection lost: try again, waiting longer each time
                backoff = min(backoff * 2, 10.0)
