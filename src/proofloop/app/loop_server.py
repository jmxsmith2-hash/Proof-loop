"""Local web app and API: the page, the live stream, and the route modules of ``routes``."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from proofloop import __version__
from proofloop.app.routes import MODULES
from proofloop.core import theme
from proofloop.loopfiles import threemf
from proofloop.loopfiles.gcode.processor import ProcessError
from proofloop.monitor.service import App

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def create_app(app: App, allow_send: bool = True) -> FastAPI:
    """``allow_send=False`` for phone access: a paired phone can watch, never start a print."""
    api = FastAPI(title="ProofLoop", version=__version__)

    def auth(request: Request, authorization: str | None = Header(default=None)):
        token = app.settings.app_token
        if not token:
            return
        given = (authorization or "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
        if given != token:
            raise HTTPException(401, "this ProofLoop needs its access token")

    guard = [Depends(auth)]

    @api.exception_handler(ProcessError)
    async def _pe(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @api.exception_handler(threemf.PlateFileError)
    async def _fe(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @api.exception_handler(KeyError)
    async def _ke(request, exc):
        return JSONResponse({"detail": str(exc).strip("'\"")}, status_code=404)

    @api.get("/", response_class=HTMLResponse)
    def index():
        return theme.inline((STATIC / "loop.html").read_text(encoding="utf-8"))

    def live_version() -> dict:
        """What the page shows, in a form that changes when it does (cheap: no files read)."""
        def mtime(path):
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        last_event = app.store.events(1)
        last_use = app.store.usage(limit=1)
        return {
            "printers": [(w.name, w.link.state.online, w.link.state.gcode_state, w.link.state.layer_num, w.phase,
                          w.status, w.waiting_for_you, w.loop_no) for w in app.watches.values()],
            "events": last_event[0]["id"] if last_event else 0,
            "queue": mtime(app.queue.path),
            "orders": mtime(app.orders.path),
            "filament": (last_use[0]["id"] if last_use else 0, len(app.inventory.all())),
        }

    @api.get("/api/live", dependencies=guard)
    async def live(request: Request, limit: int = 0):
        """Server-sent events: ``change`` with what changed (printers, events, queue, orders, filament), as it
        happens, so the page refreshes only then. ``limit`` ends the stream after that many (for tests)."""
        async def stream():
            yield "retry: 3000\n\n"
            last, sent, quiet = None, 0, 0
            while not await request.is_disconnected():
                now = live_version()
                changed = [k for k in now if last is None or now[k] != last[k]]
                if changed:  # the first time, everything
                    yield f"event: change\ndata: {json.dumps(changed)}\n\n"
                    sent, quiet = sent + 1, 0
                    if limit and sent >= limit:
                        return
                elif quiet >= 15:
                    yield ": still here\n\n"  # keeps proxies and the browser from closing an idle stream
                    quiet = 0
                last, quiet = now, quiet + 1
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.get("/js/{name}.js")
    def page_script(name: str):
        """The page's script, one file per part (static/js); no secrets in them, so no token needed."""
        path = STATIC / "js" / f"{name}.js"
        if not name.replace("-", "").isalnum() or not path.is_file():
            raise HTTPException(404, "no such script")
        return Response(path.read_bytes(), media_type="text/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    for module in MODULES:
        module.register(api, app, guard, allow_send)
    return api
