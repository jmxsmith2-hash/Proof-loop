"""Hardware profiles, the loop-file defaults, and the calibration prints."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from proofloop.app.routes.common import Guard, profile_or_404, read_upload
from proofloop.loopfiles import threemf
from proofloop.loopfiles.calibration import TESTS, build_test
from proofloop.loopfiles.importer import ImportError_, import_profile
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    @api.get("/api/profiles/{pid}", dependencies=guard)
    def get_profile(pid: str):
        return json.loads(profile_or_404(app, pid).to_json())

    @api.post("/api/profiles/import", dependencies=guard)
    async def import_(plain: UploadFile = File(...), farmloop: UploadFile = File(...), name: str = Form("")):
        raw = threemf.loads(await read_upload(plain), name=Path(plain.filename or "plain").name)
        fl = threemf.loads(await read_upload(farmloop), name=Path(farmloop.filename or "farmloop").name)
        try:
            report = import_profile(raw, fl, name.strip() or None)
            app.profiles.save(report.profile)
        except (ImportError_, ValueError) as e:
            raise HTTPException(422, str(e)) from e
        return {
            "profile": json.loads(report.profile.to_json()),
            "found": report.found,
            "dropped": report.dropped,
        }

    @api.delete("/api/profiles/{pid}", dependencies=guard)
    def delete_profile(pid: str):
        app.profiles.delete(pid)
        return {"deleted": pid}

    @api.put("/api/defaults", dependencies=guard)
    def put_defaults(body: dict):
        try:
            body = {k: v for k, v in body.items() if k != "part_push"}  # per-plate, never a default
            return app.update_defaults(body).model_dump()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @api.post("/api/calibration", dependencies=guard)
    async def calibration(
        file: UploadFile = File(...),
        test: str = Form(...),
        profile_id: str = Form(...),
        release_temp: float = Form(30.0),
    ):
        if test not in TESTS:
            raise HTTPException(422, f"unknown test {test!r}")
        sf = threemf.loads(await read_upload(file), name=Path(file.filename or "plate.gcode.3mf").name)
        data, name = build_test(sf, profile_or_404(app, profile_id), test, release_temp)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"', "X-ProofLoop-Filename": name},
        )
