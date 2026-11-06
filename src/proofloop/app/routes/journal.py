"""The print journal: how every print ended, with its file and picture."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from proofloop.app.routes.common import Guard
from proofloop.monitor.printcost import history
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    @api.get("/api/outcomes", dependencies=guard)
    def outcomes(limit: int = 200, since: float = 0, printer: str | None = None):
        rows = app.store.outcomes(since=since, limit=min(max(1, limit), 2000), printer=printer)
        for r in rows:  # local paths stay on this computer; the files are served below
            r["has_file"] = bool(r.pop("file", None))
            r["has_picture"] = bool(r.pop("picture", None) or r.pop("eject_picture", None))
        return rows

    @api.get("/api/history", dependencies=guard)
    def print_history(limit: int = 100, since: float = 0, printer: str | None = None):
        """Recent prints with grams and cost (null when unknown), and this month's totals by filament type."""
        return history(app, limit=limit, since=since, printer=printer)

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
