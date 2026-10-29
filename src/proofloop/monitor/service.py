"""Loop files and Printers while ProofLoop runs: settings, profiles, uploaded plates and watched printers."""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

from proofloop.core.notify import Notifier
from proofloop.core.paths import PRINTERS_DB
from proofloop.core.settings import Defaults, PrinterConfig, Settings, SettingsStore
from proofloop.loopfiles import threemf
from proofloop.loopfiles.gcode.routines import LoopSettings
from proofloop.loopfiles.gcode.sweep import SweepSettings
from proofloop.loopfiles.profiles import ProfileStore
from proofloop.monitor.filament import Inventory, Spool
from proofloop.monitor.journal import PrintJournal
from proofloop.monitor.spoolman import Spoolman
from proofloop.monitor.store import Store
from proofloop.monitor.tracking import FilamentTracker
from proofloop.monitor.vision import BedVerifier
from proofloop.monitor.watch import PrinterWatch

log = logging.getLogger(__name__)


def loop_settings(
    d: Defaults | dict, part_push: dict[str, float] | None = None, swept_before: bool = False
) -> LoopSettings:
    """``part_push`` holds per-part push heights for one plate; they're never saved as defaults.

    ``swept_before``: a loop has already been swept cleanly on one of your printers, so the
    automatic first-sweep safety pause is no longer needed.
    """
    d = d if isinstance(d, Defaults) else Defaults.model_validate(d)
    pause = d.pause_first_sweep == "on" or (d.pause_first_sweep == "auto" and not swept_before)
    return LoopSettings(
        pause_first_sweep=pause,
        loops=d.loops,
        release_temp=d.release_temp,
        natural_cooldown_min=d.natural_cooldown_min,
        bender_fitted=d.bender_fitted,
        cooldown_fan=d.cooldown_fan,
        wait_for_monitor=d.wait_for_monitor,
        sweep=SweepSettings(push_offset=d.push_offset, floor_z=d.floor_z, part_push_z=dict(part_push or {})),
    )


def cooldown_for(app: App, printer: str | None, minutes: float | None, sweep_s: float, door_close_s: float):
    """Cooldown seconds and where the number came from. Measured beats estimated; unknown stays unknown."""
    w = app.watches.get(printer or "")
    if w:
        t = app.store.timings(w.name)
        if "cooldown" in t:
            return t["cooldown"]["avg"], f"measured on {w.name} (average of {t['cooldown']['n']} loops)"
        if "cycle" in t:
            # Without a bender there is no T2: door open -> door close covers cooldown + sweep + T3.
            est = max(0.0, t["cycle"]["avg"] - sweep_s - door_close_s)
            return (
                est,
                f"measured on {w.name}: door open to door close, minus the sweep ({t['cycle']['n']} loops)",
            )
    if minutes is not None:
        return minutes * 60, "your estimate"
    return None, "not measured yet"


class UploadCache:
    """Recently uploaded plates, so settings can be tweaked without re-uploading.

    With a folder they are kept on disk (the last ``max_items``), so a plate stays available after a
    restart and while a plate is handed between Check and Loop files; without one, in memory.
    """

    TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

    def __init__(self, max_items: int = 8, folder: str | Path | None = None):
        self.max = max_items
        self.folder = Path(folder) if folder else None
        self._items: OrderedDict[str, tuple[float, bytes | None, str]] = OrderedDict()
        self._lock = threading.Lock()
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)
            for meta in sorted(self.folder.glob("*.json"), key=lambda f: f.stat().st_mtime):
                try:
                    name = json.loads(meta.read_text(encoding="utf-8"))["name"]
                except (OSError, ValueError, KeyError):
                    continue
                if (self.folder / f"{meta.stem}.3mf").is_file():
                    self._items[meta.stem] = (meta.stat().st_mtime, None, name)
            self._trim()

    def _trim(self) -> None:
        while len(self._items) > self.max:
            token, _ = self._items.popitem(last=False)
            if self.folder:
                for ext in (".3mf", ".json"):
                    (self.folder / f"{token}{ext}").unlink(missing_ok=True)

    def put(self, data: bytes, name: str) -> str:
        token = secrets.token_urlsafe(12)
        with self._lock:
            if self.folder:
                (self.folder / f"{token}.3mf").write_bytes(data)
                (self.folder / f"{token}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
                self._items[token] = (time.time(), None, name)
            else:
                self._items[token] = (time.time(), data, name)
            self._trim()
        return token

    def get(self, token: str) -> threemf.SlicedFile:
        data, name = self.raw(token)
        return threemf.loads(data, name=name)

    def raw(self, token: str) -> tuple[bytes, str]:
        """The uploaded bytes and file name, e.g. to hand the plate to another tool."""
        with self._lock:
            if not self.TOKEN.match(token or "") or token not in self._items:
                raise KeyError("that upload has expired; drop the file again")
            _, data, name = self._items[token]
            self._items.move_to_end(token)
            if data is None:
                try:
                    data = (self.folder / f"{token}.3mf").read_bytes()
                except OSError as e:
                    self._items.pop(token, None)
                    raise KeyError("that upload has expired; drop the file again") from e
        return data, name


class App:
    def __init__(self, data_dir: str | Path, connect: bool = True, link_factory=None):
        self.data_dir = Path(data_dir)
        self.settings_store = SettingsStore(self.data_dir)
        self.settings: Settings = self.settings_store.load()
        self.profiles = ProfileStore(self.data_dir / "profiles")
        self.store = Store(self.data_dir / PRINTERS_DB)
        self.uploads = UploadCache(40, self.data_dir / "uploads")
        self.notifier = Notifier(self.settings.notifications)
        self.connect = connect
        self.link_factory = link_factory or self._default_link
        self.watches: dict[str, PrinterWatch] = {}
        self._stop = threading.Event()
        self.inventory = Inventory(
            self.store, lambda: self.settings.filament, self._filament_event, self._spoolman_for_reports
        )
        self.tracker = FilamentTracker(self)
        self.journal = PrintJournal(self.store, self.data_dir, {p.name: p.model_id for p in self.settings.printers})
        self._migrate_cost_spools()
        for p in self.settings.printers:
            self._start_watch(p)
        if connect:
            threading.Thread(target=self._ticker, daemon=True, name="proofloop-ticker").start()

    # ---- settings ---------------------------------------------------------------
    def save_settings(self) -> None:
        self.settings_store.save(self.settings)
        self.notifier = Notifier(self.settings.notifications)

    def update_defaults(self, values: dict) -> Defaults:
        merged = {**self.settings.defaults.model_dump(), **values}
        self.settings.defaults = Defaults.model_validate(merged)
        self.save_settings()
        return self.settings.defaults

    # ---- filament ---------------------------------------------------------------
    def _filament_event(self, level: str, kind: str, message: str, printer: str | None = None) -> None:
        self._emit(self.store.add_event(printer, level, kind, message))

    def spoolman(self) -> Spoolman | None:
        url = self.settings.filament.spoolman_url
        return Spoolman(url) if url else None

    def _spoolman_for_reports(self) -> Spoolman | None:
        return self.spoolman() if self.settings.filament.spoolman_report else None

    def _migrate_cost_spools(self) -> None:
        """Spools entered in the Costs tab before the inventory existed become inventory spools."""
        old = self.settings.costs.spools
        if not old or self.inventory.all():
            return
        for sp in old:
            self.inventory.save(
                Spool(
                    name=sp.name, material=sp.type or "?", color=sp.color, price=sp.price, net_g=sp.weight_g
                )
            )
        self.settings.costs.spools, self.settings.costs.slot_spool = [], {}
        self.save_settings()

    # ---- monitor ---------------------------------------------------------------
    @staticmethod
    def _default_link(p: PrinterConfig):
        from proofloop.monitor.printer.bambu import BambuPrinter

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
            filament=self.tracker,
            journal=self.journal,
        )
        self.journal.models[p.name] = p.model_id
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
