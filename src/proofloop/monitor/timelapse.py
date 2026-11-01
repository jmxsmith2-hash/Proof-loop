"""Timelapse of a print: a camera picture every few layers while it runs, one small animation when it ends.

Off unless you turn it on (``CameraSettings.timelapse``). The pictures are taken on the watch's own
runner, never in the printer's update, and a picture the camera can't give is skipped. When a print
ends the pictures become one animated WebP (a GIF where Pillow can't write WebP), at most
``MAX_FRAMES`` of them, ``WIDTH`` pixels wide, and the pictures are deleted. The last few timelapses
of each printer are kept in ``timelapse/<printer>/``.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from PIL import Image

from proofloop.core.settings import CameraSettings
from proofloop.monitor.printer.state import PrinterState
from proofloop.monitor.watch import CAMERA_ERRORS

log = logging.getLogger(__name__)

WIDTH = 640  # pixels wide, frames and animation
MAX_FRAMES = 100  # a long print takes a picture every few more layers
MIN_FRAMES = 3  # fewer than this is not worth an animation
FRAME_MS, LAST_FRAME_MS = 100, 1500  # how long each picture shows; the finished print stays a bit longer
MAX_BYTES = 12_000_000  # an animation bigger than this is made again from fewer pictures
ID = re.compile(r"^\d{8}-\d{6}(-\d+)?$")  # a timelapse's id: when its print started


@dataclass
class _Run:
    """One print being photographed."""

    id: str
    job: str
    dir: Path
    started: float = field(default_factory=time.time)
    count: int = 0
    last_layer: int = 0
    busy: bool = False  # a picture is being fetched
    closed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


def folder_name(printer: str) -> str:
    """A printer's name as a folder name (names can hold characters a folder can't)."""
    safe = re.sub(r"[^\w.-]+", "_", printer).strip("._") or "printer"
    return safe if safe == printer else f"{safe}-{hashlib.sha1(printer.encode()).hexdigest()[:6]}"


class Timelapse:
    def __init__(self, root: str | Path, printer: str, settings: Callable[[], CameraSettings]):
        self.dir = Path(root) / folder_name(printer)
        self.printer = printer
        self.settings = settings
        self._lock = threading.RLock()
        self._run: _Run | None = None
        self._seen_layer = 0
        self._done_at: int | None = None  # layer the last print ended at: its sweep is not a new print
        try:  # pictures left by a stop in the middle of a print are of no use
            for p in self.dir.iterdir() if self.dir.is_dir() else ():
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
        except OSError:
            log.warning("%s: could not clean up timelapse pictures", printer)

    # ---- while a print runs -----------------------------------------------------------------
    def frame(self, s: PrinterState, total: int, snapshot: Callable[[], bytes], run_async: Callable) -> None:
        """Called with every update from the printer: takes a picture when it is time for one."""
        cfg = self.settings()
        with self._lock:
            layer, self._seen_layer = s.layer_num, s.layer_num
            run = self._run
            if not cfg.timelapse:
                if run:
                    self._close(run, None, snapshot, run_async)
                return
            if s.gcode_state != "RUNNING" or not layer:
                return
            if self._done_at is not None:
                if layer >= self._done_at:  # the sweep after the print that ended
                    return
                self._done_at = None
            if run and layer < run.last_layer:  # the layers started over: a new print
                self._close(run, "stopped", snapshot, run_async)
                self._done_at, run = None, None
            if run is None:
                now = datetime.now()
                job = (s.subtask_name or s.gcode_file or "").rsplit("/", 1)[-1]
                stamp = now.strftime("%Y%m%d-%H%M%S")
                n = 1
                while (self.dir / f"{stamp}.json").exists() or (self.dir / stamp).exists():
                    n += 1
                    stamp = f"{now:%Y%m%d-%H%M%S}-{n}"
                run = self._run = _Run(stamp, job, self.dir / stamp)
            every = max(cfg.timelapse_every_layers, -(-total // (MAX_FRAMES - 1)) if total else 1)
            if run.busy or (run.last_layer and layer - run.last_layer < every):
                return
            run.busy, run.last_layer = True, layer
        run_async(partial(self._take, run, snapshot, True))

    def end(self, outcome: str | None, snapshot: Callable[[], bytes], run_async: Callable) -> None:
        """The print is over (``outcome``: "finished", "failed", "stopped" (no last picture) or None to throw
        the pictures away)."""
        with self._lock:
            run = self._run
            if run is not None:
                self._close(run, outcome, snapshot, run_async)

    def _close(self, run: _Run, outcome: str | None, snapshot, run_async: Callable) -> None:
        self._run, run.closed = None, True
        self._done_at = self._seen_layer
        run_async(partial(self._finish, run, outcome, snapshot))

    # ---- pictures -----------------------------------------------------------------------------
    def _take(self, run: _Run, snapshot: Callable[[], bytes], counted: bool) -> None:
        """One picture, small, into the print's folder. Any camera or disk error skips it.
        ``counted``: it is one of the pictures taken while the print runs (the last is not)."""
        with run.lock:
            try:
                if run.closed and counted:
                    return
                with Image.open(io.BytesIO(snapshot())) as shot:
                    img = shot.convert("RGB")
                if img.width > WIDTH:
                    img = img.resize((WIDTH, max(1, round(img.height * WIDTH / img.width))), Image.Resampling.LANCZOS)
                run.dir.mkdir(parents=True, exist_ok=True)
                img.save(run.dir / f"frame-{run.count + 1:04d}.jpg", "JPEG", quality=80)
                run.count += 1
            except CAMERA_ERRORS as e:
                log.debug("%s: timelapse picture skipped: %s", self.printer, e)
            finally:
                if counted:
                    run.busy = False

    def _finish(self, run: _Run, outcome: str | None, snapshot: Callable[[], bytes]) -> None:
        if outcome in ("finished", "failed") and run.count:
            self._take(run, snapshot, False)  # how it ended
        with run.lock:
            try:
                if outcome:
                    self._make(run, outcome)
            except (OSError, ValueError) as e:
                log.warning("%s: could not make the timelapse: %s", self.printer, e)
            finally:
                shutil.rmtree(run.dir, ignore_errors=True)
        self._prune()

    def _make(self, run: _Run, outcome: str) -> None:
        names = sorted(run.dir.glob("frame-*.jpg")) if run.dir.is_dir() else []
        if len(names) < MIN_FRAMES:
            return
        while True:
            if len(names) > MAX_FRAMES:
                names = thin(names, -(-len(names) // MAX_FRAMES))
            frames = [Image.open(p).convert("RGB") for p in names]
            kind = animate(frames, self.dir / run.id)
            path = self.dir / f"{run.id}.{kind}"
            if path.stat().st_size <= MAX_BYTES or len(names) <= 2 * MIN_FRAMES:
                break
            path.unlink()
            names = thin(names, 2)
        (self.dir / f"{run.id}.json").write_text(
            json.dumps({"started": run.started, "ended": time.time(), "job": run.job, "frames": len(names),
                        "layers": run.last_layer, "outcome": outcome, "kind": kind}),
            encoding="utf-8",
        )

    def _prune(self) -> None:
        keep = max(1, self.settings().timelapse_keep)
        try:
            for item in self.listing()[keep:]:
                for p in self.dir.glob(f"{item['id']}.*"):
                    p.unlink(missing_ok=True)
        except OSError:
            log.warning("%s: could not delete old timelapses", self.printer)

    # ---- what is kept -----------------------------------------------------------------------
    def listing(self) -> list[dict]:
        """The kept timelapses, newest first."""
        out = []
        for meta in self.dir.glob("*.json") if self.dir.is_dir() else ():
            try:
                d = json.loads(meta.read_text(encoding="utf-8"))
                file = self.dir / f"{meta.stem}.{d['kind']}"
                if ID.match(meta.stem) and file.is_file():
                    out.append({"id": meta.stem, "size": file.stat().st_size, **d})
            except (OSError, ValueError, KeyError):
                continue
        return sorted(out, key=lambda d: d["started"], reverse=True)

    def file(self, id: str) -> tuple[Path, str] | None:
        """The animation of timelapse ``id`` and its media type."""
        for d in self.listing():
            if d["id"] == id:
                return self.dir / f"{id}.{d['kind']}", f"image/{d['kind']}"
        return None


def thin(names: list[Path], step: int) -> list[Path]:
    """Every ``step``-th picture, and always the last."""
    keep = names[::step]
    return keep if keep[-1] == names[-1] else keep + [names[-1]]


def animate(frames: list[Image.Image], stem: Path) -> str:
    """Write ``frames`` as an animated WebP at ``stem``.webp, or a GIF where Pillow can't; returns "webp" or "gif"."""
    ms = [FRAME_MS] * (len(frames) - 1) + [LAST_FRAME_MS]
    try:
        frames[0].save(stem.with_suffix(".webp"), "WEBP", save_all=True, append_images=frames[1:], duration=ms,
                       loop=0, quality=70, method=4)
        return "webp"
    except (OSError, ValueError, KeyError):
        stem.with_suffix(".webp").unlink(missing_ok=True)
    pal = [f.convert("P", palette=Image.Palette.ADAPTIVE, colors=128) for f in frames]
    pal[0].save(stem.with_suffix(".gif"), "GIF", save_all=True, append_images=pal[1:], duration=ms, loop=0)
    return "gif"
