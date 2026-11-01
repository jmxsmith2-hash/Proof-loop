"""Proof photos: a camera picture of each finished plate before the sweep, so every order shows what was made.

On unless you turn it off (``CameraSettings.proof_photos``). The watch asks for one picture a loop, when the
loop's last layer is done and the heaters are off (the cooldown, or with a FarmBoard its "print done" signal),
before the plate is bent and swept. The picture is taken on the watch's own runner a few seconds later (the
plate check's settle time, so the toolhead has parked), never in the printer's update, and a picture the
camera can't give is skipped. Each is a small JPEG, ``WIDTH`` pixels wide, in
``proofs/<job>/`` (the loop file's name), with a note of the printer, the loop and when it was taken. Only the
newest ``proof_keep`` are kept, over all printers and jobs; older ones are deleted.

An order's gallery is the pictures of its jobs: the same file on the same printer, while that job ran.
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from PIL import Image

from proofloop.core.settings import CameraSettings
from proofloop.monitor.timelapse import folder_name
from proofloop.monitor.tracking import job_key
from proofloop.monitor.watch import CAMERA_ERRORS

log = logging.getLogger(__name__)

WIDTH = 640  # pixels wide
QUALITY = 75  # JPEG quality: about 40 to 70 kB a picture
ON_CARD = 4  # the newest few on the printer's card
LATE_S = 120  # a picture this long after its job was seen to end still counts for it
ID = re.compile(r"^\d{8}-\d{6}-loop\d{1,4}-[0-9a-f]{6}$")  # when it was taken, the loop, and a few letters


class ProofPhotos:
    def __init__(self, root: str | Path, settings: Callable[[], CameraSettings]):
        self.dir = Path(root)
        self.settings = settings
        self._lock = threading.Lock()
        self._kept: list[tuple[dict[str, Any], Path]] | None = None  # newest first; read from disk when first needed

    # ---- taking one ---------------------------------------------------------------------------
    def take(self, printer: str, job: str, loop: int, snapshot: Callable[[], bytes], run_async: Callable,
             settle: float = 0.0) -> bool:
        """Called once a loop's parts are printed, before the sweep: one picture, taken on ``run_async`` after
        ``settle`` seconds (the toolhead parks). False when proof photos are off."""
        if not self.settings().proof_photos:
            return False
        run_async(partial(self._take, printer, job_key(job), loop, time.time(), snapshot, settle))
        return True

    def _take(self, printer: str, job: str, loop: int, taken: float, snapshot: Callable[[], bytes],
              settle: float = 0.0) -> None:
        if settle:
            time.sleep(settle)
        with self._lock:
            self._load()  # what was kept before this one, so it isn't read from disk and added twice
        try:
            with Image.open(io.BytesIO(snapshot())) as shot:
                img = shot.convert("RGB")
            if img.width > WIDTH:
                img = img.resize((WIDTH, max(1, round(img.height * WIDTH / img.width))), Image.Resampling.LANCZOS)
            pid = f"{datetime.fromtimestamp(taken):%Y%m%d-%H%M%S}-loop{loop}-{uuid.uuid4().hex[:6]}"
            folder = self.dir / folder_name(job)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{pid}.jpg"
            img.save(path, "JPEG", quality=QUALITY)
            meta = {"id": pid, "printer": printer, "job": job, "loop": loop, "taken": taken}
            (folder / f"{pid}.json").write_text(json.dumps(meta), encoding="utf-8")
        except CAMERA_ERRORS as e:  # no picture this loop; the plate check never waits on it
            log.debug("%s: proof photo skipped: %s", printer, e)
            return
        with self._lock:
            kept = self._load()
            kept.insert(0, (meta, path))
            self._prune(kept)

    def _prune(self, kept: list[tuple[dict[str, Any], Path]]) -> None:
        keep = max(1, self.settings().proof_keep)
        for _meta, path in kept[keep:]:
            try:
                path.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
                if not any(path.parent.iterdir()):
                    path.parent.rmdir()
            except OSError:
                log.warning("could not delete an old proof photo: %s", path.name)
        del kept[keep:]

    # ---- what is kept -----------------------------------------------------------------------
    def _load(self) -> list[tuple[dict[str, Any], Path]]:
        if self._kept is None:
            kept = []
            for note in self.dir.glob("*/*.json") if self.dir.is_dir() else ():
                try:
                    meta = json.loads(note.read_text(encoding="utf-8"))
                    path = note.with_suffix(".jpg")
                    if ID.match(note.stem) and meta.get("id") == note.stem and path.is_file():
                        kept.append(({k: meta.get(k) for k in ("id", "printer", "job", "loop", "taken")}, path))
                except (OSError, ValueError, AttributeError):
                    continue
            kept.sort(key=lambda m: m[0].get("taken") or 0, reverse=True)
            self._kept = kept
        return self._kept

    def listing(self, printer: str | None = None) -> list[dict[str, Any]]:
        """The kept proof photos, newest first (only ``printer``'s, if given)."""
        with self._lock:
            return [dict(m) for m, _ in self._load() if printer is None or m["printer"] == printer]

    def latest(self, printer: str, n: int = ON_CARD) -> list[dict[str, Any]]:
        return self.listing(printer)[:n]

    def file(self, pid: str) -> Path | None:
        """The picture ``pid``, or None."""
        if not ID.match(pid):
            return None
        with self._lock:
            return next((p for m, p in self._load() if m["id"] == pid), None)

    def for_jobs(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The pictures of these queued jobs, in the order they were taken: each job's file on its printer,
        from when it was sent or downloaded until it ended."""
        wanted = [(j["printer"], job_key(j["file"]), j.get("at") or 0.0, j.get("ended")) for j in jobs if j.get("file")]
        out = []
        for m in reversed(self.listing()):
            for printer, job, start, ended in wanted:
                if (m["printer"] == printer and m["job"] == job and (m["taken"] or 0) >= start
                        and (ended is None or (m["taken"] or 0) <= ended + LATE_S)):
                    out.append(m)
                    break
        return out
