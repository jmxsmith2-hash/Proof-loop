"""The ProofLoop window: synthux and ClearLoop side by side, handing plates to each other.

One process runs both. ClearLoop's app is served here (it is mounted at ``/`` and shown at
``/clearloop``); synthux's app runs on a private port and lets only this window frame it, once for
printing (CAD and G-code) and once for website tests. Plates move between them through
``/proofloop/to-clearloop`` and ``/proofloop/to-synthux``; those, and ProofLoop's own settings
(phone access, start at login, quit), answer only the ProofLoop window.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__

log = logging.getLogger(__name__)
HEADER = "X-ProofLoop"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
STATIC = Path(__file__).parent / "static"
SETUP_HINT = "run setup.bat (Windows) or sh scripts/setup_unix.sh in the ProofLoop folder"


def installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


class Shell:
    def __init__(
        self,
        port: int,
        clearloop_data: str | Path | None = None,
        synthux_config: str | Path | None = None,
        connect: bool = True,
        use_synthux: bool | None = None,
        use_clearloop: bool | None = None,
        service=None,
        link_factory=None,  # how ClearLoop talks to a printer (tests pass a fake one)
    ):
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.service = service
        self.synthux = self.synthux_server = self.synthux_db = None
        self.clearloop = None
        if clearloop_data:
            # synthux reads ClearLoop's settings, journal and notification targets too: same folder
            os.environ["CLEARLOOP_DATA"] = str(Path(clearloop_data).resolve())
        if installed("clearloop") if use_clearloop is None else use_clearloop:
            from clearloop.config import DEFAULT_DATA
            from clearloop.service import App

            self.clearloop = App(clearloop_data or DEFAULT_DATA, connect=connect, link_factory=link_factory)
        if installed("synthux") if use_synthux is None else use_synthux:
            from synthux.app.server import serve
            from synthux.core.config import load_settings
            from synthux.core.db import Database

            settings = load_settings(Path(synthux_config) if synthux_config else None)
            self.synthux_db = Database(settings.db_path)
            self.synthux_server, self.synthux = serve(settings, self.synthux_db, port=0, shell=self.origin)
            threading.Thread(
                target=self.synthux_server.serve_forever, daemon=True, name="proofloop-synthux"
            ).start()
        if not (self.synthux or self.clearloop):
            raise RuntimeError(f"neither synthux nor ClearLoop is installed: {SETUP_HINT}")
        self.api = self._build()

    @property
    def synthux_url(self) -> str | None:
        return f"http://127.0.0.1:{self.synthux.port}/" if self.synthux else None

    def page(self) -> str:
        config = {
            "print": f"{self.synthux_url}?view=print" if self.synthux else None,
            "websites": f"{self.synthux_url}?view=web" if self.synthux else None,
            "clearloop": "/clearloop?proofloop=1" if self.clearloop else None,
            "version": __version__,
            "header": HEADER,
            "service": self.service is not None,
        }
        # JSON inside <script>: "<" escaped so no value can close the tag
        blob = json.dumps(config).replace("<", "\\u003c")
        return (STATIC / "shell.html").read_text(encoding="utf-8").replace("__PROOFLOOP_CONFIG__", blob)

    def status(self) -> dict:
        from . import autostart

        svc = self.service
        return {
            "version": __version__,
            "summary": svc.summary() if svc else None,
            "background": svc is not None,
            "autostart": autostart.enabled(),
            "phone": svc.phone.status(secrets=True) if svc and svc.phone else None,
        }

    def close(self) -> None:
        if self.synthux_server:
            self.synthux_server.shutdown()
            self.synthux_server.server_close()
        if self.synthux:
            self.synthux.close()
        if self.synthux_db:
            self.synthux_db.close()
        if self.clearloop:
            self.clearloop.shutdown()

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

        async def handoff(request: Request) -> dict:
            body = await body_of(request)
            if not (self.synthux and self.clearloop):
                missing = "ClearLoop" if self.synthux else "synthux"
                raise HTTPException(404, f"{missing} isn't installed: {SETUP_HINT}")
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
            return {"proofloop": __version__, "synthux": bool(self.synthux), "clearloop": bool(self.clearloop),
                    "background": self.service is not None}

        @api.get("/proofloop/status")
        def status(request: Request):
            window_only(request)  # it holds the phone pairing code
            return self.status()

        @api.get("/proofloop/doctor", response_class=HTMLResponse)
        def doctor_page():
            from .doctor import checks, html_page

            return HTMLResponse(html_page(checks(self, port=self.port)), headers={"Cache-Control": "no-store"})

        @api.post("/proofloop/phone")
        async def phone(request: Request):
            body = await body_of(request)
            svc = needs_service()
            if not svc.phone:
                raise HTTPException(404, f"phone access shows ClearLoop, which isn't installed: {SETUP_HINT}")
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
            from . import autostart

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

        @api.post("/proofloop/to-clearloop")
        async def to_clearloop(request: Request):
            """A plate synthux checked, opened in ClearLoop's Make a loop file."""
            body = await handoff(request)
            try:
                data, name = self.synthux.job_source(str(body.get("job", "")))
            except (ValueError, OSError) as e:
                raise HTTPException(400, str(e)) from e
            return {"token": self.clearloop.uploads.put(data, name), "file": name}

        @api.post("/proofloop/to-synthux")
        async def to_synthux(request: Request):
            """A plate dropped in ClearLoop, given synthux's full print check."""
            body = await handoff(request)
            try:
                data, name = self.clearloop.uploads.raw(str(body.get("token", "")))
            except KeyError as e:
                raise HTTPException(404, str(e).strip("'\"")) from e
            try:
                job = self.synthux.submit_bytes(name, data)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            return {"job": job.id, "title": job.title}

        if self.clearloop:
            from clearloop.server import STATIC as CLEARLOOP_STATIC
            from clearloop.server import create_app

            @api.get("/clearloop", response_class=HTMLResponse)
            def clearloop_page():
                return (CLEARLOOP_STATIC / "index.html").read_text(encoding="utf-8")

            api.mount("/", create_app(self.clearloop))  # ClearLoop's /api/... (its page is at /clearloop)
        return api
