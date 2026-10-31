"""Check a loop file you already have."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, UploadFile

from proofloop.app.routes.common import Guard, read_upload
from proofloop.core import printers
from proofloop.loopfiles import threemf
from proofloop.loopfiles.lint import lint
from proofloop.loopfiles.print_checks import print_checks
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    @api.post("/api/check", dependencies=guard)
    async def check(file: UploadFile = File(...)):
        data = await read_upload(file)
        sf = threemf.loads(data, name=Path(file.filename or "file.gcode.3mf").name)
        return {
            "file": sf.name,
            "printer": printers.model_name(sf.info.printer_model_id),
            "findings": [f.as_dict() for f in lint(sf)],
            "print_findings": [f.as_dict() for f in print_checks(sf)],
        }
