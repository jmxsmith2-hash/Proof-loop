"""Where notices go and when they stay quiet.

Secrets (a token, an SMTP password, a bot token) are saved but never sent back. A page that saves a target
leaves a secret box empty to keep the saved one. A paired phone can't change targets or send a test.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from proofloop.app.routes.common import Guard
from proofloop.core import notify
from proofloop.core.settings import Notifier, QuietHours
from proofloop.monitor.service import App


def why(e: ValueError) -> str:
    """The first problem in a settings error, in plain words."""
    errors = getattr(e, "errors", None)
    if errors:
        return str(errors()[0]["msg"]).removeprefix("Value error, ")
    return str(e)


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    def window_only() -> None:
        if not allow_send:
            raise HTTPException(403, "this can only be changed in the ProofLoop window")

    def build(body: dict, old: Notifier | None) -> Notifier:
        """A target from what the page sent. An empty secret keeps ``old``'s (when it's the same kind)."""
        body = {k: v for k, v in body.items() if k != "keep"}
        if old is not None and old.kind == body.get("kind"):
            for f in notify.SECRETS:
                if not body.get(f):
                    body[f] = getattr(old, f)
        try:
            cfg = Notifier.model_validate(body)
        except ValueError as e:
            raise HTTPException(422, why(e)) from e
        if cfg.kind in ("ntfy", "discord", "webhook") and not cfg.url.startswith(("http://", "https://")):
            raise HTTPException(422, "the URL should start with http:// or https://")
        return cfg

    def target(i: int) -> Notifier:
        if not 0 <= i < len(app.settings.notifications):
            raise HTTPException(404, "no such notification target")
        return app.settings.notifications[i]

    @api.put("/api/quiet-hours", dependencies=guard)
    def put_quiet(body: dict):
        try:
            app.settings.quiet_hours = QuietHours.model_validate(body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        app.save_settings()
        return app.settings.quiet_hours.model_dump()

    @api.put("/api/notifications", dependencies=guard)
    def put_notifications(body: list[dict]):
        """Replace all targets. Each may say ``keep``: the position of the saved target whose secrets it
        keeps (default: its own position)."""
        window_only()
        old = app.settings.notifications
        out = []
        for i, item in enumerate(body):
            k = item.get("keep", i)
            out.append(build(item, old[k] if isinstance(k, int) and 0 <= k < len(old) else None))
        app.settings.notifications = out
        app.save_settings()
        return {"count": len(out)}

    @api.post("/api/notifications", dependencies=guard)
    def add_notification(body: dict):
        window_only()
        app.settings.notifications.append(build(body, None))
        app.save_settings()
        return {"count": len(app.settings.notifications), "index": len(app.settings.notifications) - 1}

    @api.put("/api/notifications/{i}", dependencies=guard)
    def change_notification(i: int, body: dict):
        window_only()
        app.settings.notifications[i] = build(body, target(i))
        app.save_settings()
        return {"count": len(app.settings.notifications)}

    @api.delete("/api/notifications/{i}", dependencies=guard)
    def remove_notification(i: int):
        window_only()
        target(i)
        del app.settings.notifications[i]
        app.save_settings()
        return {"count": len(app.settings.notifications)}

    @api.post("/api/notifications/{i}/test", dependencies=guard)
    def test_notification(i: int):
        """Send one test notice to this target, whatever its level, and say if it went through."""
        window_only()
        return notify.send_test(target(i), app.notifier.farm_name)
