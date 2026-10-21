"""The running ClearLoop app: settings, profiles, uploaded plates and monitored printers."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import threemf
from .config import Defaults, PrinterConfig, Settings, SettingsStore
from .gcode.routines import LoopSettings
from .gcode.sweep import SweepSettings
from .monitor import PrinterWatch
from .notify import Notifier
from .profiles import ProfileStore
from .store import Store
from .vision import BedVerifier

log = logging.getLogger(__name__)


def loop_settings(d: Defaults | dict, part_push: dict[str, float] | None = None) -> LoopSettings:
    """``part_push`` holds per-part push heights for one plate; they're never saved as defaults."""
    d = d if isinstance(d, Defaults) else Defaults.model_validate(d)
    return LoopSettings(
        loops=d.loops,
        release_temp=d.release_temp,
        natural_cooldown_min=d.natural_cooldown_min,
        bender_fitted=d.bender_fitted,
        cooldown_fan=d.cooldown_fan,
        wait_for_monitor=d.wait_for_monitor,
        sweep=SweepSettings(push_offset=d.push_offset, floor_z=d.floor_z, part_push_z=dict(part_push or {})),
    )


class UploadCache:
    """Recently uploaded plates, kept in memory so settings can be tweaked without re-uploading."""

    def __init__(self, max_items: int = 8):
        self.max = max_items
        self._items: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, data: bytes, name: str) -> str:
        token = secrets.token_urlsafe(12)
        with self._lock:
            self._items[token] = (time.time(), data, name)
            while len(self._items) > self.max:
                self._items.popitem(last=False)
        return token

    def get(self, token: str) -> threemf.SlicedFile:
        with self._lock:
            if token not in self._items:
                raise KeyError("that upload has expired; drop the file again")
            _, data, name = self._items[token]
            self._items.move_to_end(token)
        return threemf.loads(data, name=name)


class App:
    def __init__(self, data_dir: str | Path, connect: bool = True, link_factory=None):
        self.data_dir = Path(data_dir)
        self.settings_store = SettingsStore(self.data_dir)
        self.settings: Settings = self.settings_store.load()
        self.profiles = ProfileStore(self.data_dir / "profiles")
        self.store = Store(self.data_dir / "clearloop.db")
        self.uploads = UploadCache()
        self.notifier = Notifier(self.settings.notifications)
        self.connect = connect
        self.link_factory = link_factory or self._default_link
        self.watches: dict[str, PrinterWatch] = {}
        self._stop = threading.Event()
        for p in self.settings.printers:
            self._start_watch(p)
        if connect:
            threading.Thread(target=self._ticker, daemon=True, name="clearloop-ticker").start()

    # ---- settings ---------------------------------------------------------------
    def save_settings(self) -> None:
        self.settings_store.save(self.settings)
        self.notifier = Notifier(self.settings.notifications)

    def update_defaults(self, values: dict) -> Defaults:
        merged = {**self.settings.defaults.model_dump(), **values}
        self.settings.defaults = Defaults.model_validate(merged)
        self.save_settings()
        return self.settings.defaults

    # ---- monitor ---------------------------------------------------------------
    @staticmethod
    def _default_link(p: PrinterConfig):
        from .printer.bambu import BambuPrinter

        return BambuPrinter(p.host, p.serial, p.access_code, p.model_id, p.snapshot_url)

    def verifier(self, p: PrinterConfig) -> BedVerifier:
        return BedVerifier(self.data_dir / "references" / p.name, p.verify)

    def _start_watch(self, p: PrinterConfig) -> PrinterWatch:
        link = self.link_factory(p)
        w = PrinterWatch(
            p.name,
            link,
            self.verifier(p),
            self.store,
            self._emit,
            p.auto_resume,
            quiet_until=lambda: self.settings.quiet_hours.until(),
            settle_s=p.verify.settle_s,
        )
        link.add_listener(w.on_state)
        if self.connect:
            link.connect()
        self.watches[p.name] = w
        return w

    def _emit(self, event: dict, image: bytes | None = None) -> None:
        self.notifier.send(event, image)

    def _ticker(self) -> None:
        while not self._stop.wait(15):
            for w in list(self.watches.values()):
                try:
                    w.tick()
                except Exception:
                    log.exception("%s: tick failed", w.name)

    def add_printer(self, p: PrinterConfig) -> PrinterWatch:
        if any(x.name == p.name for x in self.settings.printers):
            raise ValueError(f"there is already a printer called {p.name!r}")
        self.settings.printers.append(p)
        self.save_settings()
        return self._start_watch(p)

    def remove_printer(self, name: str) -> None:
        w = self.watches.pop(name, None)
        if w is None:
            raise KeyError(name)
        close = getattr(w.link, "close", None)
        if close:
            close()
        self.settings.printers = [p for p in self.settings.printers if p.name != name]
        self.save_settings()

    def printer_config(self, name: str) -> PrinterConfig:
        for p in self.settings.printers:
            if p.name == name:
                return p
        raise KeyError(name)

    def shutdown(self) -> None:
        self._stop.set()
        for w in self.watches.values():
            close = getattr(w.link, "close", None)
            if close:
                close()
