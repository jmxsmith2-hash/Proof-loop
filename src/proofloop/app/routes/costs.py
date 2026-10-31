"""Your rates, and what a plate costs with them."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from proofloop.app.routes.common import Guard, plate_cost, plate_settings, profile_or_404
from proofloop.monitor.costing import CostSettings
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
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
        defaults, st = plate_settings(app, body.get("settings"))
        loops = int(body.get("loops") or defaults.loops)
        return plate_cost(app, sf, profile_or_404(app, defaults.profile_id), st, max(1, loops), body)
