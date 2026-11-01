"""Slice a model in ProofLoop: the presets to choose from (the slicing itself is in plates.py)."""

from __future__ import annotations

from fastapi import FastAPI

from proofloop.app.routes.common import Guard
from proofloop.core import printers
from proofloop.loopfiles import slicing
from proofloop.monitor.service import App


def printer_facts(app: App, opts: dict, lib) -> list[dict]:
    """Each printer of the model: its nozzle, and the filament preset for each loaded AMS tray."""
    out = []
    for cfg in app.settings.printers:
        w = app.watches.get(cfg.name)
        if w is None or cfg.model_id != opts["model_id"]:
            continue
        st = w.link.state
        size = float(getattr(st, "nozzle_diameter", 0) or 0)
        machine = next((m["name"] for m in opts["machines"] if size and m["nozzle"] and abs(float(m["nozzle"]) - size) < 1e-3
                        and not m["mine"]), "")
        trays = [{**t, "preset": slicing.filament_for_tray(lib, machine, t, opts) if machine else ""}
                 for t in st.ams.trays() if not t.get("empty")]
        out.append({"name": cfg.name, "online": st.online, "nozzle": size or None, "nozzle_flow": st.nozzle_flow,
                    "machine": machine, "trays": trays})
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
        return opts
