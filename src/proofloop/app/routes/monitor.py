"""The printer monitor: add and find printers, test the connection, pictures of the plate, resume."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

from proofloop.app.routes.common import Guard
from proofloop.core import printers
from proofloop.core.settings import PrinterConfig, Settings
from proofloop.monitor.service import App
from proofloop.monitor.watch import CAMERA_ERRORS


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    def watch(name: str):
        w = app.watches.get(name)
        if w is None:
            raise HTTPException(404, f"no printer {name!r}")
        return w

    @api.post("/api/monitor/printers", dependencies=guard)
    def add_printer(body: dict):
        try:
            p = PrinterConfig.model_validate(body)
            return app.add_printer(p).as_dict()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @api.delete("/api/monitor/printers/{name}", dependencies=guard)
    def remove_printer(name: str):
        app.remove_printer(name)
        return {"removed": name}

    @api.get("/api/discover", dependencies=guard)
    def discover_printers(seconds: float = 6.0):
        from proofloop.monitor.printer.discovery import DiscoveryError, discover

        try:
            found = discover(min(max(seconds, 2.0), 15.0))
        except DiscoveryError as e:
            raise HTTPException(503, str(e)) from e
        known = {p.serial for p in app.settings.printers}
        for f in found:
            f["model"] = (
                printers.model_name(f["model_id"]) if f["model_id"] in printers.MODEL_IDS else f["model_id"]
            )
            f["added"] = f["serial"] in known
        return found

    @api.put("/api/failure-watch", dependencies=guard)
    def put_failure_watch(body: dict):
        if not allow_send and body.get("mode") == "pause":
            raise HTTPException(403, "this can only be changed in the ProofLoop window")
        try:
            app.settings.failure_watch = Settings.model_validate({"failure_watch": body.get("mode")}).failure_watch
        except ValueError as e:
            raise HTTPException(422, "choose off, alert or pause") from e
        app.save_settings()
        return {"mode": app.settings.failure_watch}

    @api.put("/api/monitor/{name}/hands-off", dependencies=guard)
    def put_hands_off(name: str, body: dict):
        """Let this printer take its next queued job by itself once its plate checks empty."""
        if not allow_send:
            raise HTTPException(403, "this can only be changed in the ProofLoop window")
        cfg = next((p for p in app.settings.printers if p.name == name), None)
        if cfg is None:
            raise HTTPException(404, f"no printer {name!r}")
        cfg.hands_off = bool(body.get("on"))
        app.save_settings()
        return {"name": name, "hands_off": cfg.hands_off, "send_to_printer": app.settings.send_to_printer}

    @api.post("/api/monitor/{name}/test", dependencies=guard)
    def test_connection(name: str):
        """Log in to the printer the way a send does (nothing is printed or copied)."""
        from proofloop.monitor.connection import test_printer

        w = watch(name)
        p = next(p for p in app.settings.printers if p.name == name)
        r = test_printer(p, w.link)
        w.event("info" if r["ok"] else "warning", "connection_test",
                "connection test passed" if r["ok"] else "connection test: " + next(
                    s["detail"] for s in r["steps"] if not s["ok"]))
        return r

    @api.post("/api/monitor/{name}/bends/reset", dependencies=guard)
    def reset_bends(name: str):
        watch(name).reset_bends()
        return watch(name).as_dict()

    @api.get("/api/monitor/{name}/snapshot", dependencies=guard)
    def snapshot(name: str):
        try:
            return Response(watch(name).link.snapshot(), media_type="image/jpeg")
        except CAMERA_ERRORS as e:
            raise HTTPException(502, f"camera: {e}") from e

    @api.post("/api/monitor/{name}/references", dependencies=guard)
    def add_reference(name: str):
        w = watch(name)
        try:
            path = w.verifier.add_reference(w.link.snapshot())
        except CAMERA_ERRORS as e:
            raise HTTPException(502, f"camera: {e}") from e
        w.event("info", "reference", f"saved empty-plate picture {path.name}")
        return w.as_dict()

    @api.delete("/api/monitor/{name}/references", dependencies=guard)
    def clear_references(name: str):
        w = watch(name)
        w.verifier.clear_references()
        return w.as_dict()

    @api.get("/api/monitor/{name}/diff", dependencies=guard)
    def diff(name: str):
        path = app.data_dir / "references" / name / "last-diff.png"
        if not path.exists():
            raise HTTPException(404, "no plate check yet")
        return FileResponse(path, media_type="image/png")

    @api.post("/api/monitor/{name}/check", dependencies=guard)
    def check_again(name: str):
        watch(name).check_again()
        return watch(name).as_dict()

    @api.post("/api/monitor/{name}/resume", dependencies=guard)
    def resume(name: str):
        if not watch(name).resume_anyway():
            raise HTTPException(503, "not connected to the printer")
        return watch(name).as_dict()

    @api.get("/api/events", dependencies=guard)
    def events(limit: int = 60):
        return app.store.events(limit)
