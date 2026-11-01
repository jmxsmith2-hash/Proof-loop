"""Maintenance: what each printer has used since each task was done, the tasks and their intervals."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from proofloop.app.routes.common import Guard
from proofloop.monitor import maintenance as maint
from proofloop.monitor.service import App


def _422(e: ValidationError) -> HTTPException:
    first = e.errors()[0]
    where = ".".join(str(x) for x in first["loc"])
    return HTTPException(422, f"{where}: {first['msg']}" if where else first["msg"])


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    @api.get("/api/maintenance", dependencies=guard)
    def get_maintenance():
        return maint.status(app)

    @api.get("/api/maintenance/log", dependencies=guard)
    def get_log(printer: str | None = None, limit: int = 50):
        names = {t["id"]: t["name"] for t in maint.tasks(app.settings.maintenance)}
        rows = app.store.maintenance_log(printer, max(1, min(limit, 500)))
        return [{**r, "name": names.get(r["task"], r["task"])} for r in rows]

    @api.post("/api/maintenance/done", dependencies=guard)
    def done(body: dict):
        """A task was done on a printer (``date``: the day, if it wasn't today): counting starts again from there."""
        try:
            return maint.mark_done(app, str(body.get("printer") or ""), str(body.get("task") or ""),
                                   str(body.get("note") or ""), body.get("date") or None)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e
        except ValueError as e:
            raise HTTPException(422, "give the day as YYYY-MM-DD, and not a day that hasn't happened yet") from e

    @api.patch("/api/maintenance", dependencies=guard)
    def change_reminders(body: dict):
        if "remind" in body:
            app.settings.maintenance.remind = bool(body["remind"])
            app.save_settings()
        return {"remind": app.settings.maintenance.remind}

    @api.post("/api/maintenance/tasks", dependencies=guard)
    def add_task(body: dict):
        """Your own task: a name and an interval in hours, prints, loops, grams or days."""
        try:
            task = maint.CustomTask.model_validate({**body, "id": "c" + uuid.uuid4().hex[:8], "enabled": True})
        except ValidationError as e:
            raise _422(e) from e
        app.settings.maintenance.custom.append(task)
        app.save_settings()
        return next(t for t in maint.tasks(app.settings.maintenance) if t["id"] == task.id)

    @api.patch("/api/maintenance/tasks/{tid}", dependencies=guard)
    def change_task(tid: str, body: dict):
        """Change a task's interval, unit or whether it is on (a custom task's name too)."""
        cfg = app.settings.maintenance
        try:
            if tid in maint.BUILTIN_IDS:
                merged = {**(cfg.edits[tid].model_dump() if tid in cfg.edits else {}),
                          **{k: body[k] for k in ("every", "unit", "enabled") if k in body}}
                cfg.edits[tid] = maint.TaskEdit.model_validate(merged)
            else:
                i = next((n for n, c in enumerate(cfg.custom) if c.id == tid), None)
                if i is None:
                    raise HTTPException(404, f"no maintenance task {tid!r}")
                changes = {k: body[k] for k in ("name", "every", "unit", "enabled") if k in body}
                cfg.custom[i] = maint.CustomTask.model_validate({**cfg.custom[i].model_dump(), **changes})
        except ValidationError as e:
            raise _422(e) from e
        app.save_settings()
        return next(t for t in maint.tasks(cfg) if t["id"] == tid)

    @api.delete("/api/maintenance/tasks/{tid}", dependencies=guard)
    def remove_task(tid: str):
        """Remove your own task, or put a built-in one back to its starting interval."""
        cfg = app.settings.maintenance
        if tid in maint.BUILTIN_IDS:
            cfg.edits.pop(tid, None)
        elif any(c.id == tid for c in cfg.custom):
            cfg.custom = [c for c in cfg.custom if c.id != tid]
        else:
            raise HTTPException(404, f"no maintenance task {tid!r}")
        app.save_settings()
        return {"removed" if tid not in maint.BUILTIN_IDS else "reset": tid}
