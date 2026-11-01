"""Home Assistant / MQTT export: the broker to publish printer states to (off unless you turn it on).

The broker password is saved but never sent back; leave its box empty to keep the saved one. A paired
phone can see whether the export is running, nothing about the broker, and can't change it.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from proofloop.app.routes.common import Guard
from proofloop.app.routes.notifications import why
from proofloop.core.settings import MqttExport
from proofloop.monitor import homeassistant
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    def window_only() -> None:
        if not allow_send:
            raise HTTPException(403, "this can only be changed in the ProofLoop window")

    def build(body: dict) -> MqttExport:
        """Settings from what the page sent; an empty password keeps the saved one."""
        body = dict(body)
        if not body.get("password"):
            body["password"] = app.settings.mqtt_export.password
        try:
            return MqttExport.model_validate(body)
        except ValueError as e:
            raise HTTPException(422, why(e)) from e

    @api.get("/api/home-assistant", dependencies=guard)
    def get_export():
        cfg, status = app.settings.mqtt_export, app.homeassistant.status()
        if not allow_send:  # the error names the broker's address: phones get only how it's going
            return {"enabled": cfg.enabled, "status": {"state": status["state"]}}
        return {**cfg.model_dump(exclude={"password"}), "has_password": bool(cfg.password), "status": status}

    @api.put("/api/home-assistant", dependencies=guard)
    def put_export(body: dict):
        window_only()
        app.settings.mqtt_export = build(body)
        app.save_settings()
        return get_export()

    @api.post("/api/home-assistant/test", dependencies=guard)
    def test_export(body: dict):
        """Log in to the broker with these settings (saved or not) and hang up. Publishes nothing."""
        window_only()
        return homeassistant.check_broker(build(body))
