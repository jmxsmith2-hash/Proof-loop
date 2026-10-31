"""What happened on the printers, and when the daily summary is sent."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from proofloop.app.routes.common import Guard
from proofloop.core.settings import Settings
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
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
