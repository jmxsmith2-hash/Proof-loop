"""Wire the configuration, printers, ejectors and controllers together."""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from . import threemf
from .config import Config, PrinterConfig
from .ejector import Ejector, build_ejector
from .gcode.heightmap import ObjectBox
from .gcode.processor import ProcessOptions, ProcessResult, UnsupportedMaterial, analyze, process
from .gcode.routines import recovery_gcode
from .gcode.sweep import SweepSettings, plan_sweep
from .loop import JobRejected, LoopController, PrinterLink
from .notify import Notifier
from .schedule import in_quiet_hours
from .store import Store
from .vision import BedVerifier

log = logging.getLogger(__name__)


def process_options(
    cfg: Config, p: PrinterConfig, ejector: Ejector | None = None, copies: int = 1
) -> ProcessOptions:
    loop = cfg.loop_for(p)
    mode = (
        ejector.gcode_mode if ejector else {"gcode": "gcode", "custom": "custom"}.get(p.ejector.kind, "board")
    )
    custom = (ejector.custom_gcode if ejector else p.ejector.gcode) or ""
    return ProcessOptions(
        printer=p.profile(),
        loop=loop.to_settings(mode, custom),
        copies=copies,
        bed_temp=loop.bed_temp,
        secret=cfg.farm.secret,
    )


def make_verifier(cfg: Config, p: PrinterConfig) -> BedVerifier | None:
    v = cfg.verify_for(p)
    if not v.enabled:
        return None
    return BedVerifier(cfg.data_path / "references" / p.name, v)


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s).strip("-")[:40] or "job"


class Farm:
    def __init__(self, cfg: Config, links: dict[str, PrinterLink] | None = None, connect: bool = True):
        self.cfg = cfg
        self.store = Store(cfg.data_path / "clearloop.db")
        self.notifier = Notifier(cfg.notifications, cfg.farm.name)
        self.controllers: dict[str, LoopController] = {}
        self.links: dict[str, PrinterLink] = {}
        self._stop = threading.Event()
        for p in cfg.printers:
            if not p.enabled:
                continue
            link = (links or {}).get(p.name) or self._make_link(p)
            self.links[p.name] = link
            self.controllers[p.name] = self._make_controller(p, link)
        if connect:
            for name, link in self.links.items():
                link.add_listener(self.controllers[name].on_state)
                link.connect()

    # ---- construction --------------------------------------------------------
    def _make_link(self, p: PrinterConfig) -> PrinterLink:
        from .printer.bambu import BambuPrinter

        return BambuPrinter(
            p.host, p.serial, p.access_code, p.camera_kind(), p.snapshot_url, p.start_url_template, p.use_ams
        )

    def verifier_for(self, p: PrinterConfig) -> BedVerifier | None:
        return make_verifier(self.cfg, p)

    def _make_controller(self, p: PrinterConfig, link: PrinterLink) -> LoopController:
        ejector = build_ejector(p.ejector)
        loop = self.cfg.loop_for(p)

        def prepare(job: dict) -> tuple[str, bytes, int]:
            return self.prepare_job(p, ejector, job)

        def recovery(job: dict | None) -> tuple[str, bytes]:
            return self.recovery_job(p, ejector, job)

        return LoopController(
            name=p.name,
            model=p.profile().model,
            link=link,
            ejector=ejector,
            store=self.store,
            prepare_job=prepare,
            recovery_job=recovery,
            verifier=self.verifier_for(p),
            emit=self.notifier.send,
            quiet_mode=self.quiet_mode,
            max_retries=p.max_retries,
            bend_window_s=loop.bend_window_s,
            quiet_profile=p.ejector.quiet_profile,
            plate_type=p.plate.type,
            rated_cycles=p.plate.rated_cycles,
            marker_base=loop.marker_base,
            diff_dir=self.cfg.data_path / "diffs",
        )

    # ---- job preparation -------------------------------------------------------
    def prepare_job(self, p: PrinterConfig, ejector: Ejector, job: dict) -> tuple[str, bytes, int]:
        try:
            sliced = threemf.load(job["file_path"], plate=job["plate"])
            result: ProcessResult = process(sliced, process_options(self.cfg, p, ejector))
        except (UnsupportedMaterial, ValueError, FileNotFoundError) as e:
            raise JobRejected(str(e)) from e
        for w in result.warnings:
            self.store.add_event(p.name, "info", "process_warning", f"job {job['id']}: {w}")
        ext = ".gcode.3mf" if sliced.container == "3mf" else ".gcode"
        remote = f"cl-{job['id']}-{_slug(job['name'])}{ext}"
        return remote, result.data, sliced.plate if sliced.container == "3mf" else 0

    def recovery_job(self, p: PrinterConfig, ejector: Ejector, job: dict | None) -> tuple[str, bytes]:
        opts = process_options(self.cfg, p, ejector)
        objects: list[ObjectBox] = []
        if job:
            try:
                sliced = threemf.load(job["file_path"], plate=job["plate"])
                objects = analyze(sliced, opts)[2]
            except Exception as e:  # fall back to a blind full-plate wipe
                log.warning("recovery: could not re-read job file: %s", e)
        settings = SweepSettings(**{**opts.loop.sweep.__dict__, "final_wipe": True})
        plan = plan_sweep(objects, opts.printer, settings)
        return f"cl-recovery-{_slug(p.name)}.gcode", recovery_gcode(opts.printer, opts.loop, plan).encode()

    # ---- schedule --------------------------------------------------------------
    def quiet_mode(self) -> str | None:
        q = self.cfg.quiet_hours
        return q.mode if q and in_quiet_hours(q) else None

    # ---- runtime ---------------------------------------------------------------
    def run_forever(self) -> None:
        while not self._stop.is_set():
            for c in self.controllers.values():
                try:
                    c.tick()
                except Exception:
                    log.exception("%s: tick failed", c.name)
            self._stop.wait(1.0)

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, daemon=True, name="clearloop-ticker")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
        for link in self.links.values():
            close = getattr(link, "close", None)
            if close:
                close()

    # ---- queue helpers ---------------------------------------------------------
    def library_dir(self) -> Path:
        d = self.cfg.data_path / "library"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def enqueue_file(
        self,
        path: str | Path,
        name: str | None = None,
        copies: int = 1,
        printer: str | None = None,
        models: list[str] | None = None,
        priority: int = 0,
        order_ref: str | None = None,
        sku: str | None = None,
        plate: int | None = None,
    ) -> list[int]:
        """Queue ``copies`` plates of a sliced file. Material requirements come from the file."""
        sliced = threemf.load(path, plate=plate)
        ids = []
        for _ in range(max(1, copies)):
            ids.append(
                self.store.add_job(
                    name or Path(path).name,
                    str(path),
                    sliced.plate,
                    printer,
                    models or [],
                    sliced.info.filaments,
                    priority,
                    order_ref,
                    sku,
                )
            )
        self.store.add_event(
            None,
            "info",
            "queued",
            f"queued {len(ids)} x {name or Path(path).name}",
            {"jobs": ids, "order": order_ref},
        )
        return ids

    def stats(self, days: float = 30) -> dict:
        return self.store.stats(since=time.time() - days * 86400)
