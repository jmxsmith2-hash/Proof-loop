"""Everything the page needs to draw itself, in one call."""

from __future__ import annotations

from fastapi import FastAPI, Request

from proofloop import __version__
from proofloop.app.routes.common import Guard
from proofloop.core import printers
from proofloop.core.notify import public_view
from proofloop.monitor.service import App


def shown(n, role: str) -> dict:
    """A notification target as the page sees it: never a secret (only whether one is saved). A phone gets
    even less, without the address, mail addresses or chat id: whoever holds a webhook address can post to
    your channel."""
    return public_view(n, window=role == "window")


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
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
    def state(request: Request):
        role = getattr(request.state, "role", "window")  # "watch" / "operator" on a phone (app/phone.py)
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
            "send_confirm": app.settings.send_confirm,
            "can_send": allow_send,
            "role": role,
            "notifications": [shown(n, role if allow_send else "phone") for n in app.settings.notifications],
            "quiet_hours": app.settings.quiet_hours.model_dump(),
            "failure_watch": app.settings.failure_watch,
            "camera": app.settings.camera.model_dump(),
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
