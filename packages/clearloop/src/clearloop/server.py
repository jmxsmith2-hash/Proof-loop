"""Local web app and API."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import __version__, printers, threemf
from .calibration import TESTS, build_test
from .config import Defaults, Notifier, PrinterConfig, QuietHours
from .gcode.processor import ProcessError, analyze, changes, compatibility, process, process_batch
from .importer import ImportError_, import_profile
from .lint import lint
from .service import App, loop_settings

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 300 * 1024 * 1024


def _settings(app: App, body: dict | None) -> tuple:
    body = dict(body or {})
    raw = body.pop("part_push", None) or {}
    try:
        part_push = {str(k): float(v) for k, v in raw.items() if v is not None}
    except (TypeError, ValueError, AttributeError) as e:
        raise HTTPException(422, "part_push must map part labels to heights in mm") from e
    if any(not 0 <= z <= 1000 for z in part_push.values()):
        raise HTTPException(422, "push heights must be between 0 and 1000 mm")
    defaults = Defaults.model_validate({**app.settings.defaults.model_dump(), **body})
    return defaults, loop_settings(defaults, part_push)


def create_app(app: App) -> FastAPI:
    api = FastAPI(title="ClearLoop", version=__version__)

    def auth(authorization: str | None = Header(default=None), request: Request = None):
        token = app.settings.app_token
        if not token:
            return
        given = (authorization or "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
        if given != token:
            raise HTTPException(401, "this ClearLoop needs its access token")

    guard = [Depends(auth)]

    async def read_upload(f: UploadFile) -> bytes:
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "file is larger than 300 MB")
        return data

    def profile(pid: str):
        try:
            return app.profiles.get(pid)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    def analysis_payload(token: str, body: dict | None) -> dict:
        sf = app.uploads.get(token)
        defaults, st = _settings(app, body)
        prof = profile(defaults.profile_id)
        a = analyze(sf, prof, st)
        out = a.as_dict()
        out["token"] = token
        out["settings"] = defaults.model_dump()
        out["profile"] = {
            "id": prof.id,
            "name": prof.name,
            "stage": prof.stage,
            "has_bender_steps": prof.has_bender_steps,
            "sweep": {"y_rear": prof.sweep.y_rear, "y_front": prof.sweep.y_front},
        }
        return out

    @api.exception_handler(ProcessError)
    async def _pe(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @api.exception_handler(threemf.PlateFileError)
    async def _fe(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @api.exception_handler(KeyError)
    async def _ke(request, exc):
        return JSONResponse({"detail": str(exc).strip("'\"")}, status_code=404)

    @api.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text()

    @api.get("/api/state", dependencies=guard)
    def state():
        return {
            "version": __version__,
            "defaults": app.settings.defaults.model_dump(),
            "profiles": [
                {
                    "id": p.id,
                    "name": p.name,
                    "printer": printers.model_name(p.printer_model_id),
                    "printer_model_id": p.printer_model_id,
                    "stage": p.stage,
                    "builtin": p.source.get("kind") == "builtin",
                    "description": p.description,
                    "has_bender_steps": p.has_bender_steps,
                }
                for p in app.profiles.all()
            ],
            "printers": {k: v for k, v in printers.MODEL_IDS.items()},
            "monitor": [w.as_dict() for w in app.watches.values()],
            "notifications": [n.model_dump(exclude={"token"}) for n in app.settings.notifications],
            "quiet_hours": app.settings.quiet_hours.model_dump(),
        }

    # ---- make a loop file ---------------------------------------------------------
    @api.post("/api/plates", dependencies=guard)
    async def upload_plate(file: UploadFile = File(...)):
        data = await read_upload(file)
        sf = threemf.loads(data, name=Path(file.filename or "plate.gcode.3mf").name)
        token = app.uploads.put(data, sf.name)
        # Pick a profile for this printer: the saved default if it fits, otherwise the first match.
        matching = app.profiles.for_printer(sf.info.printer_model_id)
        if not matching:
            return {
                "token": token,
                "printer": printers.model_name(sf.info.printer_model_id),
                "printer_model_id": sf.info.printer_model_id,
                "no_profile": True,
            }
        pid = app.settings.defaults.profile_id
        if pid not in {p.id for p in matching}:
            pid = matching[0].id
        return analysis_payload(token, {"profile_id": pid})

    @api.post("/api/plates/{token}/analyze", dependencies=guard)
    def reanalyze(token: str, body: dict):
        return analysis_payload(token, body)

    @api.post("/api/plates/{token}/download", dependencies=guard)
    def download(token: str, body: dict):
        sf = app.uploads.get(token)
        defaults, st = _settings(app, body)
        r = process(sf, profile(defaults.profile_id), st)
        return Response(
            r.data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{r.filename}"',
                "X-ClearLoop-Filename": r.filename,
            },
        )

    @api.post("/api/plates/{token}/changes", dependencies=guard)
    def plate_changes(token: str, body: dict):
        sf = app.uploads.get(token)
        defaults, st = _settings(app, body)
        return changes(sf, profile(defaults.profile_id), st)

    # ---- batch: several plates in one file --------------------------------------------
    def batch_items(body: dict):
        items = []
        for it in body.get("items", []):
            try:
                items.append((app.uploads.get(str(it["token"])), int(it.get("count", 1))))
            except (KeyError, TypeError, ValueError) as e:
                raise HTTPException(
                    422, "each batch item needs a token from an uploaded plate and a count"
                ) from e
        if not items:
            raise HTTPException(422, "add plates to the batch first")
        return items

    @api.post("/api/batch/preview", dependencies=guard)
    def batch_preview(body: dict):
        items = batch_items(body)
        defaults, st = _settings(app, body.get("settings"))
        prof = profile(defaults.profile_id)
        first = items[0][0]
        rows, problems = [], []
        for sf, n in items:
            row = {
                "file": sf.name,
                "count": n,
                "parts": 0,
                "print_seconds": sf.info.print_seconds,
                "problems": [],
            }
            row["problems"] += compatibility(first, sf) if sf is not first else []
            try:
                a = analyze(sf, prof, st)
                row["parts"] = len(a.parts)
                row["problems"] += a.problems
                row["sweep_s"] = round(a.sweep_s)
            except ProcessError as e:
                row["problems"].append(str(e))
            problems += row["problems"]
            rows.append(row)
        return {
            "items": rows,
            "loops": sum(n for _, n in items),
            "parts": sum(r["parts"] * r["count"] for r in rows),
            "print_seconds": sum(r["print_seconds"] * r["count"] for r in rows),
            "ok": not problems,
        }

    @api.post("/api/batch/download", dependencies=guard)
    def batch_download(body: dict):
        items = batch_items(body)
        defaults, st = _settings(app, body.get("settings"))
        r = process_batch(items, profile(defaults.profile_id), st)
        return Response(
            r.data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{r.filename}"',
                "X-ClearLoop-Filename": r.filename,
            },
        )

    # ---- check a file -------------------------------------------------------------
    @api.post("/api/check", dependencies=guard)
    async def check(file: UploadFile = File(...)):
        data = await read_upload(file)
        sf = threemf.loads(data, name=Path(file.filename or "file.gcode.3mf").name)
        return {
            "file": sf.name,
            "printer": printers.model_name(sf.info.printer_model_id),
            "findings": [f.as_dict() for f in lint(sf)],
        }

    # ---- profiles -------------------------------------------------------------------
    @api.get("/api/profiles/{pid}", dependencies=guard)
    def get_profile(pid: str):
        return json.loads(profile(pid).to_json())

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

    # ---- defaults & calibration ----------------------------------------------------------
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
        data, name = build_test(sf, profile(profile_id), test, release_temp)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"', "X-ClearLoop-Filename": name},
        )

    # ---- monitor -----------------------------------------------------------------------
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
        from .printer.discovery import DiscoveryError, discover

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

    @api.put("/api/quiet-hours", dependencies=guard)
    def put_quiet(body: dict):
        try:
            app.settings.quiet_hours = QuietHours.model_validate(body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        app.save_settings()
        return app.settings.quiet_hours.model_dump()

    @api.post("/api/monitor/{name}/bends/reset", dependencies=guard)
    def reset_bends(name: str):
        watch(name).reset_bends()
        return watch(name).as_dict()

    @api.get("/api/monitor/{name}/snapshot", dependencies=guard)
    def snapshot(name: str):
        try:
            return Response(watch(name).link.snapshot(), media_type="image/jpeg")
        except Exception as e:
            raise HTTPException(502, f"camera: {e}") from e

    @api.post("/api/monitor/{name}/references", dependencies=guard)
    def add_reference(name: str):
        w = watch(name)
        try:
            path = w.verifier.add_reference(w.link.snapshot())
        except Exception as e:
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

    @api.put("/api/notifications", dependencies=guard)
    def put_notifications(body: list[dict]):
        try:
            app.settings.notifications = [Notifier.model_validate(n) for n in body]
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        app.save_settings()
        return {"count": len(app.settings.notifications)}

    return api
