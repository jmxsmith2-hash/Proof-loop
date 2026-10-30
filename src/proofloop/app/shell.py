"""The ProofLoop window: Check, Loop files and Printers in one page, handing plates around.

One process and one server run everything: the window at ``/``, the Check page and its API under
``/check``, and the Loop files & Printers page at ``/loop`` with its API mounted at ``/``. Plates
move between the tabs through ``/proofloop/to-loop`` and ``/proofloop/to-check``; those, Check's
uploads, and the window's own settings (phone access, start at login, quit) answer only the
ProofLoop window.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from proofloop import __version__
from proofloop.core import theme
from proofloop.core.paths import home

log = logging.getLogger(__name__)
HEADER = "X-ProofLoop"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
STATIC = Path(__file__).parent / "static"


class Shell:
    def __init__(
        self,
        port: int,
        config: str | Path | None = None,  # the config folder (default ~/.proofloop/config)
        connect: bool = True,
        service=None,
        link_factory=None,  # how the monitor talks to a printer (tests pass a fake one)
    ):
        from proofloop.app.checks import Checks
        from proofloop.core.config import load_settings
        from proofloop.core.db import Database
        from proofloop.monitor.service import App

        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.service = service
        self.monitor = App(home(), connect=connect, link_factory=link_factory)
        settings = load_settings(Path(config) if config else None)
        self.results_db = Database(settings.db_path)
        self.checks = Checks(settings, self.results_db)
        self.api = self._build()

    def page(self) -> str:
        config = {
            "check": "/check",
            "loop": "/loop?proofloop=1",
            "version": __version__,
            "header": HEADER,
            "service": self.service is not None,
            "pretend": bool(os.environ.get("PROOFLOOP_PRETEND")),  # proofloop try
        }
        # JSON inside <script>: "<" escaped so no value can close the tag
        blob = json.dumps(config).replace("<", "\\u003c")
        return theme.inline((STATIC / "shell.html").read_text(encoding="utf-8")).replace("__PROOFLOOP_CONFIG__", blob)

    def status(self) -> dict:
        from proofloop.app import autostart

        svc = self.service
        return {
            "version": __version__,
            "summary": svc.summary() if svc else None,
            "background": svc is not None,
            "autostart": autostart.enabled(),
            "phone": svc.phone.status(secrets=True) if svc else None,
        }

    def close(self) -> None:
        self.checks.close()
        self.results_db.close()
        self.monitor.shutdown()

    def _build(self) -> FastAPI:
        api = FastAPI(title="ProofLoop", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
        api.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

        def window_only(request: Request) -> None:
            """Only the ProofLoop window itself (or a program on this computer) may change things."""
            origin = request.headers.get("origin")
            if request.headers.get(HEADER) != "1" or (origin and origin != self.origin):
                raise HTTPException(403, "requests must come from the ProofLoop window")

        async def body_of(request: Request) -> dict:
            window_only(request)
            try:
                body = await request.json()
            except ValueError as e:
                raise HTTPException(400, "send JSON") from e
            if not isinstance(body, dict):
                raise HTTPException(400, "send a JSON object")
            return body

        def needs_service():
            if self.service is None:
                raise HTTPException(409, "ProofLoop isn't running in the background (started with proofloop app)")
            return self.service

        @api.get("/", response_class=HTMLResponse)
        def index():
            return HTMLResponse(
                self.page(), headers={"X-Frame-Options": "DENY", "Cache-Control": "no-store"}
            )

        @api.get("/proofloop/ping")
        def ping():
            return {"proofloop": __version__, "background": self.service is not None}

        @api.get("/proofloop/status")
        def status(request: Request):
            window_only(request)  # it holds the phone pairing code
            return self.status()

        @api.get("/proofloop/guide")
        def guide(request: Request):
            from proofloop.app import guide

            window_only(request)
            return guide.summary(self)

        @api.get("/proofloop/doctor", response_class=HTMLResponse)
        def doctor_page():
            from proofloop.app.doctor import checks, html_page

            return HTMLResponse(html_page(checks(self, port=self.port)), headers={"Cache-Control": "no-store"})

        @api.post("/proofloop/phone")
        async def phone(request: Request):
            body = await body_of(request)
            svc = needs_service()
            if body.get("forget"):
                svc.settings.new_phone_secrets()
            if "on" in body:
                if body["on"]:
                    try:
                        svc.phone.start()
                    except RuntimeError as e:
                        raise HTTPException(409, str(e)) from e
                else:
                    svc.phone.stop()
                svc.settings.phone = bool(body["on"])
            svc.settings.save()
            return self.status()

        @api.post("/proofloop/autostart")
        async def set_autostart(request: Request):
            from proofloop.app import autostart

            body = await body_of(request)
            try:
                autostart.enable() if body.get("on") else autostart.disable()
            except Exception as e:  # noqa: BLE001
                raise HTTPException(500, f"couldn't change it: {e}") from e
            return self.status()

        @api.post("/proofloop/quit")
        async def quit_(request: Request):
            body = await body_of(request)
            needs_service().stop_soon(str(body.get("reason") or "you chose Quit"))
            return {"stopping": True}

        @api.post("/proofloop/to-loop")
        async def to_loop(request: Request):
            """A plate checked under Check, opened in Make a loop file."""
            body = await body_of(request)
            try:
                data, name = self.checks.job_source(str(body.get("job", "")))
            except (ValueError, OSError) as e:
                raise HTTPException(400, str(e)) from e
            return {"token": self.monitor.uploads.put(data, name), "file": name}

        @api.post("/proofloop/to-check")
        async def to_check(request: Request):
            """A plate dropped in Make a loop file, given the full print check."""
            body = await body_of(request)
            try:
                data, name = self.monitor.uploads.raw(str(body.get("token", "")))
            except KeyError as e:
                raise HTTPException(404, str(e).strip("'\"")) from e
            try:
                job = self.checks.submit_bytes(name, data)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            return {"job": job.id, "title": job.title}

        from proofloop.app import checks
        from proofloop.app.loop_server import STATIC as LOOP_STATIC
        from proofloop.app.loop_server import create_app

        checks.register(api, self.checks, window_only)

        @api.get("/loop", response_class=HTMLResponse)
        def loop_page():
            return theme.inline((LOOP_STATIC / "loop.html").read_text(encoding="utf-8"))

        api.mount("/", create_app(self.monitor))  # Loop files' and Printers' /api/... (the page is at /loop)
        return api
