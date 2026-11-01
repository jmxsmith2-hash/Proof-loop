"""The watch folder: save a model into a folder and it's sliced and waiting in Make a loop file.

ProofLoop looks in the folder (``Downloads/ProofLoop`` unless you chose another) every few seconds.
A new model (``.stl``, ``.obj``, ``.step``) or Bambu Studio project is sliced with the presets you
last used in ProofLoop's Slice card, made to fit the printer you chose for the folder: its nozzle
size and flow, and the filament in its AMS when yours doesn't fit. An already sliced plate is taken
as it is. Each one is listed under *Make a loop file* to open, and you're told when it's ready.

Files are only read: nothing in the folder is moved, changed or deleted. A file is taken once it has
stopped changing for a few seconds (so a download in progress isn't sliced half-written), and a file
saved again with new contents is taken again.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from proofloop.core import printers
from proofloop.loopfiles import slicing

if TYPE_CHECKING:
    from proofloop.monitor.service import App

log = logging.getLogger(__name__)

EVERY_S = 3.0
SETTLE_S = 3.0  # unchanged this long before it's taken
KEEP = 30  # items listed
SUFFIXES = (*slicing.MESH_SUFFIXES, ".3mf")
MAX_BYTES = 200 * 1024 * 1024


def default_folder() -> Path:
    return Path.home() / "Downloads" / "ProofLoop"


def sweep_clear_y(app: App, model_id: str) -> float:
    """The Y that parts stay in front of, so the strip the pusher comes down in stays clear (0: no profile)."""
    from proofloop.loopfiles.api import pick_profile
    from proofloop.loopfiles.gcode.sweep import SweepSettings

    profile = pick_profile(app.profiles, model_id, app.settings.defaults.profile_id)
    return profile.sweep.y_rear - SweepSettings().rear_clearance - 2 if profile is not None else 0.0


class FolderWatch:
    def __init__(self, app: App):
        self.app = app
        self.state_path = app.data_dir / "watch_folder.json"
        self._lock = threading.Lock()
        self.items: list[dict[str, Any]] = []
        self.seen: dict[str, float] = {}  # "name|size|mtime" -> when it was taken
        try:
            doc = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.items, self.seen = list(doc.get("items") or []), dict(doc.get("seen") or {})
        except (OSError, ValueError, AttributeError):
            pass

    def folder(self) -> Path:
        p = self.app.settings.watch_folder.path.strip()
        return Path(p).expanduser() if p else default_folder()

    def ensure_folder(self) -> None:
        """The default folder is made for you; a folder you named has to exist."""
        if not self.app.settings.watch_folder.path.strip():
            default_folder().mkdir(parents=True, exist_ok=True)

    def as_dict(self) -> dict[str, Any]:
        cfg = self.app.settings.watch_folder
        with self._lock:
            items = [dict(i) for i in self.items]
        return {**cfg.model_dump(), "folder": str(self.folder()), "exists": self.folder().is_dir(), "items": items,
                "usual": self.app.settings.slice_usual.model_dump()}

    def _save(self) -> None:
        self.seen = dict(sorted(self.seen.items(), key=lambda kv: kv[1])[-500:])
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": self.items, "seen": self.seen}), encoding="utf-8")
        tmp.replace(self.state_path)

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(EVERY_S):
            try:
                self.scan()
            except Exception:  # broad: the watch-folder thread must never die
                log.exception("the watch folder check failed")

    def scan(self, now: float | None = None) -> list[dict[str, Any]]:
        """Take every new, settled file in the folder. Returns what was taken this time."""
        if not self.app.settings.watch_folder.on:
            return []
        now = time.time() if now is None else now
        folder = self.folder()
        if not folder.is_dir():
            return []
        taken = []
        for f in sorted(folder.iterdir(), key=lambda f: f.name.lower()):
            if not f.is_file() or not f.name.lower().endswith(SUFFIXES) or f.name.startswith((".", "~")):
                continue
            st = f.stat()
            key = f"{f.name}|{st.st_size}|{int(st.st_mtime)}"
            if key in self.seen or now - st.st_mtime < SETTLE_S:
                continue
            self.seen[key] = now
            item = self.take(f, st.st_size)
            with self._lock:
                self.items = [item, *self.items][:KEEP]
                self._save()
            taken.append(item)
        return taken

    def take(self, f: Path, size: int) -> dict[str, Any]:
        item: dict[str, Any] = {"file": f.name, "at": time.time(), "status": "failed", "token": None, "name": "",
                                "notes": [], "error": ""}
        try:
            if size > MAX_BYTES:
                raise slicing.SliceError(f"it's over {MAX_BYTES // (1024 * 1024)} MB")
            data = f.read_bytes()
            kind = slicing.kind_of(f.name, data)
            if kind == "plate":
                item.update(status="ready", name=f.name, token=self.app.uploads.put(data, f.name),
                            notes=["already sliced"])
            elif kind:
                sliced, name, notes = self.slice(data, f.name)
                item.update(status="ready", name=name, token=self.app.uploads.put(sliced, name), notes=notes)
            else:
                raise slicing.SliceError("it isn't a model ProofLoop can slice")
        except (slicing.SliceError, OSError) as e:
            item["error"] = str(e)
        if item["status"] == "ready":
            msg = f"{f.name} from your watch folder is sliced and waiting in Make a loop file"
            if item["notes"] and item["notes"] != ["already sliced"]:
                msg += f" ({'; '.join(item['notes'])})"
            self.app._emit(self.app.store.add_event(None, "info", "watch_folder", msg))
        else:
            self.app._emit(self.app.store.add_event(None, "warning", "watch_folder",
                                                    f"{f.name} from your watch folder couldn't be sliced: {item['error']}"))
        return item

    def slice(self, data: bytes, name: str) -> tuple[bytes, str, list[str]]:
        found, lib = slicing.slicer_and_presets()
        cfg = self.app.settings.watch_folder
        mine = [p for p in self.app.settings.printers if not cfg.printer or p.name == cfg.printer]
        p = mine[0] if mine else None
        model_id = p.model_id if p else printers.P2S
        opts = slicing.options(lib, model_id)
        w = self.app.watches.get(p.name) if p else None
        facts = slicing.printer_facts(p.name, w.link.state, opts, lib) if p and w else None
        choice = slicing.usual_choice(self.app.settings.slice_usual.model_dump(), facts, opts)
        choice.clear_behind_y = sweep_clear_y(self.app, model_id)
        work = Path(self.app.data_dir) / "slice-watch"
        try:
            return slicing.slice_upload(data, name, choice, work, model_id=model_id, found=found, lib=lib)
        finally:
            shutil.rmtree(work, ignore_errors=True)
