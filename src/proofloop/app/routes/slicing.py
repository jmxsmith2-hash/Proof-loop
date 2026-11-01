"""Slice a model in ProofLoop: the presets to choose from, and the watch folder (the slicing itself is in plates.py)."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from proofloop.app.routes.common import Guard
from proofloop.core import printers
from proofloop.core.settings import WatchFolder
from proofloop.loopfiles import slicing
from proofloop.monitor.service import App


def printer_facts(app: App, opts: dict, lib) -> list[dict]:
    """Each printer of the model: its nozzle, and the filament preset for each loaded AMS tray."""
    out = []
    for cfg in app.settings.printers:
        w = app.watches.get(cfg.name)
        if w is None or cfg.model_id != opts["model_id"]:
            continue
        out.append(slicing.printer_facts(cfg.name, w.link.state, opts, lib))
    return out


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    @api.get("/api/slice/options", dependencies=guard)
    def slice_options(model_id: str = printers.P2S):
        """What a model can be sliced with here: the slicer, its presets for the printer model, your printers."""
        try:
            found, lib = slicing.slicer_and_presets()
        except slicing.SliceError as e:
            return {"slicer": None, "error": str(e)}
        opts = slicing.options(lib, model_id)
        opts["model_id"] = model_id
        opts["slicer"] = found.label
        opts["printers"] = printer_facts(app, opts, lib)
        opts["usual"] = app.settings.slice_usual.model_dump()
        return opts

    @api.get("/api/watch-folder", dependencies=guard)
    def watch_folder():
        """The watch folder's settings and what it has sliced lately."""
        return app.folder_watch.as_dict()

    @api.put("/api/watch-folder", dependencies=guard)
    def put_watch_folder(body: dict):
        if not allow_send:
            raise HTTPException(403, "the watch folder can only be set in the ProofLoop window")
        try:
            cfg = WatchFolder.model_validate({**app.settings.watch_folder.model_dump(), **body})
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if cfg.printer and cfg.printer not in {p.name for p in app.settings.printers}:
            raise HTTPException(422, f"no printer called {cfg.printer}")
        app.settings.watch_folder = cfg
        app.save_settings()
        if cfg.on:
            app.folder_watch.ensure_folder()
        return app.folder_watch.as_dict()
