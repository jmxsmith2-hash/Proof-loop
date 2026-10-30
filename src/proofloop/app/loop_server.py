"""Local web app and API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from proofloop import __version__
from proofloop.core import printers, theme
from proofloop.core.settings import Defaults, Notifier, PrinterConfig, QuietHours, Settings
from proofloop.loopfiles import threemf
from proofloop.loopfiles.api import pick_profile
from proofloop.loopfiles.calibration import TESTS, build_test
from proofloop.loopfiles.gcode.processor import (
    ProcessError,
    analyze,
    changes,
    compatibility,
    dwell_seconds,
    process,
    process_batch,
)
from proofloop.loopfiles.importer import ImportError_, import_profile
from proofloop.loopfiles.lint import lint
from proofloop.loopfiles.print_checks import print_checks
from proofloop.monitor.costing import CostSettings, Times, cost
from proofloop.monitor.filament import prices_for, requirements
from proofloop.monitor.queue import spread
from proofloop.monitor.send import SendError
from proofloop.monitor.service import App, cooldown_for, loop_settings
from proofloop.monitor.tracking import register_job

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 300 * 1024 * 1024


def _part_push(raw) -> dict[str, float]:
    try:
        part_push = {str(k): float(v) for k, v in (raw or {}).items() if v is not None}
    except (TypeError, ValueError, AttributeError) as e:
        raise HTTPException(422, "part_push must map part labels to heights in mm") from e
    if any(not 0 <= z <= 1000 for z in part_push.values()):
        raise HTTPException(422, "push heights must be between 0 and 1000 mm")
    return part_push


def _settings(app: App, body: dict | None) -> tuple:
    body = dict(body or {})
    part_push = _part_push(body.pop("part_push", None))
    defaults = Defaults.model_validate({**app.settings.defaults.model_dump(), **body})
    return defaults, loop_settings(defaults, part_push, app.store.has_good_loop())


def _with_push(st, part_push: dict[str, float]):
    """The same settings with one plate's own per-part push heights."""
    return replace(st, sweep=replace(st.sweep, part_push_z=dict(part_push)))


def create_app(app: App, allow_send: bool = True) -> FastAPI:
    """``allow_send=False`` for phone access: a paired phone can watch, never start a print."""
    api = FastAPI(title="ProofLoop", version=__version__)

    def auth(authorization: str | None = Header(default=None), request: Request = None):
        token = app.settings.app_token
        if not token:
            return
        given = (authorization or "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
        if given != token:
            raise HTTPException(401, "this ProofLoop needs its access token")

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
        out["first_pause"] = st.pause_first_sweep  # what "auto" works out to right now
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
        return theme.inline((STATIC / "loop.html").read_text(encoding="utf-8"))

    def live_version() -> dict:
        """What the page shows, in a form that changes when it does (cheap: no files read)."""
        def mtime(path):
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        last_event = app.store.events(1)
        last_use = app.store.usage(limit=1)
        return {
            "printers": [(w.name, w.link.state.online, w.link.state.gcode_state, w.link.state.layer_num, w.phase,
                          w.status, w.waiting_for_you, w.loop_no) for w in app.watches.values()],
            "events": last_event[0]["id"] if last_event else 0,
            "queue": mtime(app.queue.path),
            "orders": mtime(app.orders.path),
            "filament": (last_use[0]["id"] if last_use else 0, len(app.inventory.all())),
        }

    @api.get("/api/live", dependencies=guard)
    async def live(request: Request, limit: int = 0):
        """Server-sent events: ``change`` with what changed (printers, events, queue, orders, filament), as it
        happens, so the page refreshes only then. ``limit`` ends the stream after that many (for tests)."""
        async def stream():
            yield "retry: 3000\n\n"
            last, sent, quiet = None, 0, 0
            while not await request.is_disconnected():
                now = live_version()
                changed = [k for k in now if last is None or now[k] != last[k]]
                if changed:  # the first time, everything
                    yield f"event: change\ndata: {json.dumps(changed)}\n\n"
                    sent, quiet = sent + 1, 0
                    if limit and sent >= limit:
                        return
                elif quiet >= 15:
                    yield ": still here\n\n"  # keeps proxies and the browser from closing an idle stream
                    quiet = 0
                last, quiet = now, quiet + 1
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.get("/js/{name}.js")
    def page_script(name: str):
        """The page's script, one file per part (static/js); no secrets in them, so no token needed."""
        path = STATIC / "js" / f"{name}.js"
        if not name.replace("-", "").isalnum() or not path.is_file():
            raise HTTPException(404, "no such script")
        return Response(path.read_bytes(), media_type="text/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    def monitor_card(w) -> dict:
        d = w.as_dict()
        _key, job = app.tracker.job_for(w.link.state)
        d["job_loops"] = job["loops"] if job else None
        linked = {
            sp.tray: sp
            for sp in app.inventory.all(archived=False)
            if sp.printer == w.name and sp.tray is not None
        }
        for t in d["printer"]["ams"]["trays"]:
            sp = linked.get(t["tray"])
            t["spool"] = {"id": sp.id, "label": sp.label, "remaining_g": sp.remaining_g} if sp else None
        return d

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
            "monitor": [monitor_card(w) for w in app.watches.values()],
            "send_to_printer": app.settings.send_to_printer and allow_send,
            "can_send": allow_send,
            "notifications": [n.model_dump(exclude={"token"}) for n in app.settings.notifications],
            "quiet_hours": app.settings.quiet_hours.model_dump(),
            "failure_watch": app.settings.failure_watch,
            "costs": app.settings.costs.model_dump(),
            "filament": app.settings.filament.model_dump(),
            "spools": [
                {
                    "id": sp.id,
                    "label": sp.label,
                    "material": sp.material,
                    "color": sp.color,
                    "remaining_g": sp.remaining_g,
                    "per_kg": sp.per_kg,
                }
                for sp in app.inventory.all(archived=False)
            ],
        }

    # ---- make a loop file ---------------------------------------------------------
    @api.post("/api/plates", dependencies=guard)
    async def upload_plate(file: UploadFile = File(...)):
        data = await read_upload(file)
        sf = threemf.loads(data, name=Path(file.filename or "plate.gcode.3mf").name)
        return open_plate(app.uploads.put(data, sf.name), sf)

    @api.post("/api/plates/{token}/open", dependencies=guard)
    def open_uploaded(token: str):
        """A plate put in the upload cache by another part of ProofLoop (a plate handed over from Check)."""
        return open_plate(token, app.uploads.get(token))

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

    @api.post("/api/plates/{token}/analyze", dependencies=guard)
    def reanalyze(token: str, body: dict):
        return analysis_payload(token, body)

    def made_file(sf, body: dict):
        """The loop file, recorded (for filament counting and the journal) before it leaves ProofLoop."""
        defaults, st = _settings(app, body)
        r = process(sf, profile(defaults.profile_id), st)
        register_job(app.store, r.filename, [(sf, st.loops, len(r.analysis.parts))], defaults.profile_id)
        app.journal.keep_file(r.filename, r.data)
        return r

    def send_made(name: str, confirm, r) -> dict:
        if not allow_send:
            raise HTTPException(403, "files can't be sent to printers from here")
        if not name or confirm != name:
            raise HTTPException(400, "confirm the printer to send it to")
        made = threemf.loads(r.data, name=r.filename)
        danger = [f.title for f in lint(made) if f.level == "danger"]
        try:
            return app.send(name, r.filename, r.data, made, danger)
        except SendError as e:
            raise HTTPException(409, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, f"no printer called {name!r}") from e

    def file_response(r) -> Response:
        return Response(
            r.data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{r.filename}"', "X-ProofLoop-Filename": r.filename},
        )

    @api.post("/api/plates/{token}/download", dependencies=guard)
    def download(token: str, body: dict):
        return file_response(made_file(app.uploads.get(token), body))

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
        return send_made(name, confirm, made_file(app.uploads.get(token), body))

    # ---- spread a run over your printers, and the queue --------------------------------
    def plan_for(sf, parts, settings: dict) -> dict:
        try:
            parts = int(parts or 0)
        except (TypeError, ValueError):
            parts = 0
        if not 1 <= parts <= 100000:
            raise HTTPException(422, "say how many parts you need (1 to 100000)")
        defaults, st = _settings(app, {**(settings or {}), "loops": 1})
        prof = profile(defaults.profile_id)
        an = analyze(sf, prof, st)
        if not an.ok:
            raise HTTPException(422, "fix the problems with this plate first")
        known = sum(s.get("seconds") or 0 for s in an.steps)
        return spread(app, sf, prof.printer_model_id, known, len(an.parts), parts)

    @api.post("/api/plates/{token}/spread", dependencies=guard)
    def spread_run(token: str, body: dict):
        return plan_for(app.uploads.get(token), body.get("parts"), body.get("settings") or {})

    @api.get("/api/queue", dependencies=guard)
    def queue():
        return app.queue.runs()

    @api.post("/api/queue", dependencies=guard)
    def queue_add(body: dict):
        token = str(body.get("token", ""))
        sf = app.uploads.get(token)
        plan = plan_for(sf, body.get("parts"), body.get("settings") or {})
        data, name = app.uploads.raw(token)
        try:
            return app.queue.add(name, data, body.get("settings") or {}, plan)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    def queued_file(rid: str, jid: str):
        run, job = app.queue.get(rid), app.queue.job(rid, jid)
        data, name = app.queue.plate(rid)
        return job, made_file(threemf.loads(data, name=name), {**run["settings"], "loops": job["loops"]})

    @api.post("/api/queue/{rid}/{jid}/download", dependencies=guard)
    def queue_download(rid: str, jid: str):
        job, r = queued_file(rid, jid)
        app.queue.mark(rid, jid, status="downloaded", file=r.filename, at=time.time(), why=None, loop=None)
        return file_response(r)

    @api.post("/api/queue/{rid}/{jid}/send", dependencies=guard)
    def queue_send(rid: str, jid: str, body: dict):
        job = app.queue.job(rid, jid)
        if not allow_send:
            raise HTTPException(403, "files can't be sent to printers from here")
        if body.get("confirm") != job["printer"]:
            raise HTTPException(400, "confirm the printer to send it to")
        job, r = queued_file(rid, jid)
        result = send_made(job["printer"], body.get("confirm"), r)
        app.queue.mark(rid, jid, status="sent", file=r.filename, at=time.time(), why=None, loop=None)
        return result

    @api.delete("/api/queue/{rid}", dependencies=guard)
    def queue_remove(rid: str):
        app.queue.get(rid)
        app.queue.remove(rid)
        return {"removed": rid}

    @api.get("/api/summary", dependencies=guard)
    def get_summary(hours: float = 12):
        from proofloop.monitor.summary import summary

        return {**summary(app, hours=max(1.0, min(hours, 24 * 31))), "at": app.settings.summary_at}

    @api.put("/api/summary-at", dependencies=guard)
    def put_summary_at(body: dict):
        """The time of day the summary is sent (``None``: not sent)."""
        try:
            app.settings.summary_at = Settings.model_validate({"summary_at": body.get("at") or None}).summary_at
        except ValueError as e:
            raise HTTPException(422, "give a time like 07:30") from e
        app.save_settings()
        return {"at": app.settings.summary_at}

    # ---- orders -----------------------------------------------------------------------
    @api.get("/api/orders", dependencies=guard)
    def list_orders():
        from proofloop.monitor.orders import progress

        out = [progress(app, o) for o in app.orders.all()]
        return sorted(out, key=lambda o: (o["status"] != "open", o["due"] or "9999", o["created"]))

    @api.post("/api/orders", dependencies=guard)
    def add_order(body: dict):
        """Plan the run for an order, keep it as a queue (nothing starts), and follow it."""
        from proofloop.monitor.orders import due_ts, progress

        token = str(body.get("token", ""))
        sf = app.uploads.get(token)
        qty = body.get("quantity")
        try:
            if body.get("due"):
                due_ts(str(body["due"]))
        except ValueError as e:
            raise HTTPException(422, "give the due date as YYYY-MM-DD") from e
        settings = body.get("settings") or {}
        plan = plan_for(sf, qty, settings)
        data, name = app.uploads.raw(token)
        price = body.get("price_each")
        try:
            price = None if price in (None, "") else float(price)
            defaults, st = _settings(app, {**settings, "loops": 1})
            per_part = plate_cost(sf, profile(defaults.profile_id), st, 1, {}, setup=False)["totals"]["per_part"]
            run = app.queue.add(name, data, settings, plan)
            order = app.orders.add(str(body.get("customer") or ""), name, int(qty), body.get("due") or None, run,
                                   plan, price, per_part, str(body.get("notes") or ""))
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return progress(app, order)

    @api.patch("/api/orders/{oid}", dependencies=guard)
    def change_order(oid: str, body: dict):
        from proofloop.monitor.orders import due_ts, progress

        changes = {k: body[k] for k in ("customer", "due", "price_each", "notes", "status") if k in body}
        if changes.get("status") not in (None, "open", "done", "cancelled"):
            raise HTTPException(422, "status is open, done or cancelled")
        try:
            if changes.get("due"):
                due_ts(changes["due"])
            return progress(app, app.orders.update(oid, **changes))
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, "give the due date as YYYY-MM-DD") from e

    @api.delete("/api/orders/{oid}", dependencies=guard)
    def remove_order(oid: str, keep_run: bool = False):
        try:
            gone = app.orders.remove(oid)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        if not keep_run:
            try:
                app.queue.remove(gone["run"])
            except KeyError:
                pass
        return {"removed": oid}

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
        defaults, st = _settings(app, body)
        return changes(sf, profile(defaults.profile_id), st)

    # ---- costs -------------------------------------------------------------------------
    def trays_of(printer: str | None) -> list[dict]:
        w = app.watches.get(printer or "")
        return w.link.state.ams.trays() if w else []

    def slot_prices(sf, printer: str | None) -> dict:
        return prices_for(app.inventory, printer, sf.info.filament_slots, trays_of(printer))

    def plate_cost(sf, prof, st, loops: int, body: dict, setup: bool = True):
        a = analyze(sf, prof, st)
        fixed = sum(x["seconds"] or 0 for x in a.steps if x["key"] != "print")
        cool, src = cooldown_for(
            app,
            body.get("cooldown_printer"),
            body.get("cooldown_min"),
            a.sweep_s,
            dwell_seconds(prof.door_close),
        )
        c = app.settings.costs if setup else app.settings.costs.model_copy(update={"labor_min_per_run": None})
        times = Times(sf.info.print_seconds, fixed, cool, src)
        printer = body.get("printer") or body.get("cooldown_printer")
        return cost(
            c,
            sf.info.filament_slots,
            sf.info.slot_grams,
            len(a.parts),
            times,
            loops,
            slot_prices(sf, printer),
        ).as_dict()

    @api.post("/api/plates/{token}/filament", dependencies=guard)
    def plate_filament(token: str, body: dict):
        sf = app.uploads.get(token)
        printer = body.get("printer") or None
        if printer and printer not in app.watches:
            raise HTTPException(404, f"no printer {printer!r}")
        loops = max(1, int(body.get("loops") or app.settings.defaults.loops))
        out = requirements(
            app.inventory,
            printer,
            sf.info.filament_slots,
            sf.info.slot_grams,
            loops,
            trays_of(printer),
            sf.info.presets,
        )
        out["printer"] = printer
        return out

    @api.put("/api/costs", dependencies=guard)
    def put_costs(body: dict):
        try:
            app.settings.costs = CostSettings.model_validate(body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        app.save_settings()
        return app.settings.costs.model_dump()

    @api.post("/api/plates/{token}/cost", dependencies=guard)
    def cost_of_plate(token: str, body: dict):
        sf = app.uploads.get(token)
        defaults, st = _settings(app, body.get("settings"))
        loops = int(body.get("loops") or defaults.loops)
        return plate_cost(sf, profile(defaults.profile_id), st, max(1, loops), body)

    # ---- batch: several plates in one file --------------------------------------------
    def batch_items(body: dict):
        """(plate, count, that plate's own per-part push heights) in print order."""
        items = []
        for it in body.get("items", []):
            try:
                items.append((app.uploads.get(str(it["token"])), int(it.get("count", 1)), it))
            except (KeyError, TypeError, ValueError) as e:
                raise HTTPException(
                    422, "each batch item needs a token from an uploaded plate and a count"
                ) from e
        items = [(sf, n, _part_push(it.get("part_push"))) for sf, n, it in items]
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
        for sf, n, pp in items:
            row = {
                "file": sf.name,
                "count": n,
                "parts": 0,
                "print_seconds": sf.info.print_seconds,
                "problems": [],
            }
            row["problems"] += compatibility(first, sf) if sf is not first else []
            try:
                a = analyze(sf, prof, _with_push(st, pp))
                row["parts"] = len(a.parts)
                row["custom_heights"] = len(a.plan.part_custom)
                row["problems"] += a.problems
                row["sweep_s"] = round(a.sweep_s)
            except ProcessError as e:
                row["problems"].append(str(e))
            problems += row["problems"]
            rows.append(row)
        return {
            "items": rows,
            "notes": batch_notes(items),
            "loops": sum(n for _, n, _ in items),
            "parts": sum(r["parts"] * r["count"] for r in rows),
            "print_seconds": sum(r["print_seconds"] * r["count"] for r in rows),
            "ok": not problems,
            "cost": batch_cost(items, prof, st, body) if not problems else None,
        }

    def batch_notes(items) -> list[str]:
        notes = []
        plates = list({id(sf): sf for sf, _, _ in items}.values())
        if len(plates) > 1:
            notes.append(
                "The printer's screen and Bambu Studio show the first plate's picture, print time and filament "
                "for the whole file, as with the FarmLoop app's multi-loop files. ProofLoop's printer monitor follows "
                "each plate."
            )
            seen: dict[str, str] = {}
            shared = set()
            for sf in plates:
                for label in sf.info.object_heights or {}:
                    if label in seen and seen[label] != sf.name:
                        shared.add(label)
                    seen.setdefault(label, sf.name)
            if shared:
                notes.append(
                    f"Some plates use the same object ids ({', '.join(sorted(shared)[:6])}). Don't use "
                    "'skip object' on the printer for this file: it would skip parts on the other plates too."
                )
        return notes

    def batch_cost(items, prof, st, body: dict) -> dict:
        costs = [plate_cost(sf, prof, st, n, body, setup=False) for sf, n, _ in items]
        c = app.settings.costs
        setup = (
            c.labor_min_per_run / 60 * c.labor_rate if None not in (c.labor_rate, c.labor_min_per_run) else 0
        )
        total = sum(x["totals"]["run"] for x in costs) + setup
        parts = sum(x["totals"]["parts"] for x in costs)
        return {
            "currency": c.currency,
            "run": round(total, 4),
            "per_part": round(total / max(1, parts), 4),
            "complete": all(x["totals"]["complete"] for x in costs),
        }

    @api.post("/api/batch/download", dependencies=guard)
    def batch_download(body: dict):
        items = batch_items(body)
        defaults, st = _settings(app, body.get("settings"))
        prof = profile(defaults.profile_id)
        r = process_batch(items, prof, st)
        register_job(
            app.store,
            r.filename,
            [(sf, n, len(analyze(sf, prof, _with_push(st, pp)).parts)) for sf, n, pp in items],
            prof.id,
        )
        app.journal.keep_file(r.filename, r.data)
        return Response(
            r.data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{r.filename}"',
                "X-ProofLoop-Filename": r.filename,
            },
        )

    # ---- print journal --------------------------------------------------------------
    @api.get("/api/outcomes", dependencies=guard)
    def outcomes(limit: int = 200, since: float = 0, printer: str | None = None):
        rows = app.store.outcomes(since=since, limit=min(max(1, limit), 2000), printer=printer)
        for r in rows:  # local paths stay on this computer; the files are served below
            r["has_file"] = bool(r.pop("file", None))
            r["has_picture"] = bool(r.pop("picture", None) or r.pop("eject_picture", None))
        return rows

    @api.get("/api/outcomes/{oid}/file", dependencies=guard)
    def outcome_file(oid: int):
        data = app.store.outcome(oid)
        path = Path(data["file"]) if data and data.get("file") else None
        if path is None or not path.is_file() or (app.data_dir / "files").resolve() not in path.resolve().parents:
            raise HTTPException(404, "no copy of that file")
        return Response(path.read_bytes(), media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

    @api.get("/api/outcomes/{oid}/picture", dependencies=guard)
    def outcome_picture(oid: int):
        data = app.store.outcome(oid) or {}
        raw = data.get("picture") or data.get("eject_picture")
        path = Path(raw) if raw else None
        if path is None or not path.is_file() or (app.data_dir / "outcomes").resolve() not in path.resolve().parents:
            raise HTTPException(404, "no picture")
        return Response(path.read_bytes(), media_type="image/jpeg")

    # ---- check a file -------------------------------------------------------------
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
            headers={"Content-Disposition": f'attachment; filename="{name}"', "X-ProofLoop-Filename": name},
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

    @api.put("/api/quiet-hours", dependencies=guard)
    def put_quiet(body: dict):
        try:
            app.settings.quiet_hours = QuietHours.model_validate(body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        app.save_settings()
        return app.settings.quiet_hours.model_dump()

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

    from proofloop.app.loop_routes import register

    register(api, app, guard, window=allow_send)
    return api
