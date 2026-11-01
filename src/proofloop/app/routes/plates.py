"""Make a loop file: upload a plate, look at it, change its settings, download or send the result."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from proofloop.app.routes.common import (
    Guard,
    file_response,
    made_file,
    plate_settings,
    profile_or_404,
    read_upload,
    send_made,
)
from proofloop.core import printers
from proofloop.loopfiles import threemf
from proofloop.loopfiles.api import pick_profile
from proofloop.loopfiles.gcode.processor import analyze, changes
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    def analysis_payload(token: str, body: dict | None) -> dict:
        sf = app.uploads.get(token)
        defaults, st = plate_settings(app, body)
        prof = profile_or_404(app, defaults.profile_id)
        a = analyze(sf, prof, st)
        out = a.as_dict()
        out["token"] = token
        out["settings"] = defaults.model_dump()
        out["first_pause"] = st.pause_first_sweep  # what "auto" works out to right now
        out["profile"] = {
            "id": prof.id,
            "name": prof.name,
            "stage": prof.stage,
            "has_bender_steps": prof.has_bender_steps,
            "sweep": {"y_rear": prof.sweep.y_rear, "y_front": prof.sweep.y_front},
        }
        return out

    def open_plate(token: str, sf) -> dict:
        profile = pick_profile(app.profiles, sf.info.printer_model_id, app.settings.defaults.profile_id)
        if profile is None:
            return {
                "token": token,
                "printer": printers.model_name(sf.info.printer_model_id),
                "printer_model_id": sf.info.printer_model_id,
                "no_profile": True,
            }
        return analysis_payload(token, {"profile_id": profile.id})

    @api.post("/api/plates", dependencies=guard)
    async def upload_plate(file: UploadFile = File(...)):
        data = await read_upload(file)
        name = Path(file.filename or "plate.gcode.3mf").name
        sf = threemf.loads(data, name=name)
        numbers = threemf.sliced_plates(data)
        if len(numbers) < 2:
            return open_plate(app.uploads.put(data, sf.name), sf)
        # 'Export all sliced file': each plate is kept as its own file, so everything after this
        # (settings, batch, queue, sending) works on one plate at a time.
        plates: list[dict] = []
        for n in numbers:
            own = threemf.plate_name(name, n)
            plates.append({"plate": n, "file": own, "token": app.uploads.put(threemf.only_plate(data, n), own)})
        token = plates[0]["token"]
        out = open_plate(token, app.uploads.get(token))
        out["plates"] = plates
        return out

    @api.post("/api/plates/{token}/open", dependencies=guard)
    def open_uploaded(token: str):
        """A plate put in the upload cache by another part of ProofLoop (a plate handed over from Check)."""
        return open_plate(token, app.uploads.get(token))

    @api.post("/api/plates/{token}/analyze", dependencies=guard)
    def reanalyze(token: str, body: dict):
        return analysis_payload(token, body)

    @api.post("/api/plates/{token}/download", dependencies=guard)
    def download(token: str, body: dict):
        return file_response(made_file(app, app.uploads.get(token), body))

    @api.post("/api/plates/{token}/send", dependencies=guard)
    def send(token: str, body: dict):
        """Make the loop file, as Download does, and send it to a printer you confirmed."""
        body = dict(body)
        name, confirm = str(body.pop("printer", "") or ""), body.pop("confirm", None)
        if not allow_send:
            raise HTTPException(403, "files can't be sent to printers from here")
        if not name or confirm != name:
            raise HTTPException(400, "confirm the printer to send it to")
        # Recorded before the printer starts, so filament counting and the monitor recognise it.
        return send_made(app, allow_send, name, confirm, made_file(app, app.uploads.get(token), body))

    @api.post("/api/plates/{token}/printers", dependencies=guard)
    def send_targets(token: str):
        """Which printers this plate could be sent to right now, and why not for the others."""
        if not allow_send:
            raise HTTPException(403, "files can't be sent to printers from here")
        return {"send_to_printer": app.settings.send_to_printer, "printers": app.send_readiness(app.uploads.get(token))}

    @api.put("/api/send-to-printer", dependencies=guard)
    def put_send(body: dict):
        if not allow_send:
            raise HTTPException(403, "this can only be changed in the ProofLoop window")
        app.settings.send_to_printer = bool(body.get("on"))
        app.save_settings()
        return {"send_to_printer": app.settings.send_to_printer}

    @api.post("/api/plates/{token}/changes", dependencies=guard)
    def plate_changes(token: str, body: dict):
        sf = app.uploads.get(token)
        defaults, st = plate_settings(app, body)
        return changes(sf, profile_or_404(app, defaults.profile_id), st)
