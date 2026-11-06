"""What more than one route module needs: uploads, a plate's settings, making and sending a loop file."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import HTTPException, UploadFile, params
from fastapi.responses import Response

from proofloop.core.settings import Defaults
from proofloop.loopfiles import threemf
from proofloop.loopfiles.gcode.processor import (
    Result,
    analyze,
    clean_input,
    dwell_seconds,
    process,
)
from proofloop.loopfiles.lint import lint
from proofloop.monitor.costing import Times, cost
from proofloop.monitor.filament import prices_for
from proofloop.monitor.queue import spread
from proofloop.monitor.send import SendError
from proofloop.monitor.service import App, cooldown_for, loop_settings
from proofloop.monitor.tracking import register_job

MAX_UPLOAD = 300 * 1024 * 1024

Guard = list[params.Depends]  # what ``dependencies=`` takes: the access-token check


async def read_upload(f: UploadFile) -> bytes:
    data = await f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file is larger than 300 MB")
    return data


def part_push(raw: Any) -> dict[str, float]:
    try:
        pushes = {str(k): float(v) for k, v in (raw or {}).items() if v is not None}
    except (TypeError, ValueError, AttributeError) as e:
        raise HTTPException(422, "part_push must map part labels to heights in mm") from e
    if any(not 0 <= z <= 1000 for z in pushes.values()):
        raise HTTPException(422, "push heights must be between 0 and 1000 mm")
    return pushes


def plate_settings(app: App, body: dict | None) -> tuple:
    """(defaults, loop settings) for a request: the saved defaults with the request's changes on top."""
    body = dict(body or {})
    pushes = part_push(body.pop("part_push", None))
    defaults = Defaults.model_validate({**app.settings.defaults.model_dump(), **body})
    return defaults, loop_settings(defaults, pushes, app.store.has_good_loop())


def with_push(st, pushes: dict[str, float]):
    """The same settings with one plate's own per-part push heights."""
    return replace(st, sweep=replace(st.sweep, part_push_z=dict(pushes)))


def profile_or_404(app: App, pid: str):
    try:
        return app.profiles.get(pid)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


def file_response(r) -> Response:
    return Response(
        r.data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{r.filename}"', "X-ProofLoop-Filename": r.filename},
    )


def plain_file(sf) -> Result:
    """The plate as sliced, with no cooldown or sweep (ProofLoop's own additions are taken out)."""
    gcode = clean_input(sf)
    return Result(sf.to_bytes(gcode), gcode, sf.name, None)  # type: ignore[arg-type]


def made_file(app: App, sf, body: dict):
    """The loop file, recorded (for filament counting and the journal) before it leaves ProofLoop.

    A single print (1 loop) with ``auto_eject`` off is the plate as sliced, nothing added; with it on (the
    default) it is one loop with the cooldown and sweep, so the bed is clear when it finishes.
    """
    defaults, st = plate_settings(app, body)
    prof = profile_or_404(app, defaults.profile_id)
    if st.loops == 1 and not defaults.auto_eject:
        r = plain_file(sf)
        register_job(app.store, r.filename, [(sf, 1, len(sf.info.object_names))], defaults.profile_id)
    else:
        r = process(sf, prof, st)
        register_job(app.store, r.filename, [(sf, st.loops, len(r.analysis.parts))], defaults.profile_id)
    app.journal.keep_file(r.filename, r.data)
    return r


def send_made(app: App, allow_send: bool, name: str, confirm, r) -> dict:
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


def plan_for(app: App, sf, parts, settings: dict) -> dict:
    """How a run of ``parts`` parts spreads over the printers that can print this plate."""
    try:
        parts = int(parts or 0)
    except (TypeError, ValueError):
        parts = 0
    if not 1 <= parts <= 100000:
        raise HTTPException(422, "say how many parts you need (1 to 100000)")
    defaults, st = plate_settings(app, {**(settings or {}), "loops": 1})
    prof = profile_or_404(app, defaults.profile_id)
    an = analyze(sf, prof, st)
    if not an.ok:
        raise HTTPException(422, "fix the problems with this plate first")
    known = sum(s.get("seconds") or 0 for s in an.steps)
    return spread(app, sf, prof.printer_model_id, known, len(an.parts), parts)


def trays_of(app: App, printer: str | None) -> list[dict]:
    w = app.watches.get(printer or "")
    return w.link.state.ams.trays() if w else []


def plate_cost(app: App, sf, prof, st, loops: int, body: dict, setup: bool = True):
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
    prices = prices_for(app.inventory, printer, sf.info.filament_slots, trays_of(app, printer))
    return cost(
        c,
        sf.info.filament_slots,
        sf.info.slot_grams,
        len(a.parts),
        times,
        loops,
        prices,
    ).as_dict()
