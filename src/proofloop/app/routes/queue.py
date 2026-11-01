"""Spread a run over your printers, and the queue of jobs it makes."""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException

from proofloop.app.routes.common import Guard, file_response, made_file, plan_for, send_made
from proofloop.loopfiles import threemf
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    @api.post("/api/plates/{token}/spread", dependencies=guard)
    def spread_run(token: str, body: dict):
        return plan_for(app, app.uploads.get(token), body.get("parts"), body.get("settings") or {})

    plates: dict[str, tuple[list, dict]] = {}  # run id -> (filament slots, grams per slot), read from its plate once

    def plate_of(rid: str) -> tuple[list, dict]:
        if rid not in plates:
            data, name = app.queue.plate(rid)
            info = threemf.loads(data, name=name).info  # a damaged plate is a ValueError: no forecast for it
            plates[rid] = (info.filament_slots, info.slot_grams)
        return plates[rid]

    @api.get("/api/queue", dependencies=guard)
    def queue():
        runs = app.queue.runs()
        app.tracker.queue_supply(runs, plate_of)  # will each job's trays last it?
        return runs

    @api.post("/api/queue", dependencies=guard)
    def queue_add(body: dict):
        token = str(body.get("token", ""))
        sf = app.uploads.get(token)
        plan = plan_for(app, sf, body.get("parts"), body.get("settings") or {})
        data, name = app.uploads.raw(token)
        try:
            return app.queue.add(name, data, body.get("settings") or {}, plan)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    def queued_file(rid: str, jid: str):
        run, job = app.queue.get(rid), app.queue.job(rid, jid)
        data, name = app.queue.plate(rid)
        return job, made_file(app, threemf.loads(data, name=name), {**run["settings"], "loops": job["loops"]})

    if allow_send:  # only the window's API sends; the hands-off queue makes its files the same way
        app.hands_off.make = lambda rid, jid: queued_file(rid, jid)[1]

    @api.post("/api/queue/{rid}/{jid}/hold", dependencies=guard)
    def queue_hold(rid: str, jid: str, body: dict):
        """Hold a job back from the hands-off queue (or let it go again)."""
        job = app.queue.job(rid, jid)
        if job["status"] != "waiting":
            raise HTTPException(409, "only a waiting job can be held")
        app.queue.mark(rid, jid, held=bool(body.get("held", True)), auto_at=None)
        return app.queue.job(rid, jid)

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
        result = send_made(app, allow_send, job["printer"], body.get("confirm"), r)
        app.queue.mark(rid, jid, status="sent", file=r.filename, at=time.time(), why=None, loop=None, auto_at=None)
        return result

    @api.delete("/api/queue/{rid}", dependencies=guard)
    def queue_remove(rid: str):
        app.queue.get(rid)
        app.queue.remove(rid)
        return {"removed": rid}
