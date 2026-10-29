"""Phone access: Loop files & Printers (printer status, loop files, filament, costs) on your phone.

A second listener on your local network serves the Loop files & Printers page and API only: never
Check or Website tests, never the window's handoffs or settings. It shares the one running printer
monitor, so the printers are still watched once. A phone pairs once, by scanning the QR code in the ProofLoop window (a link holding a
long secret) or by typing the six-digit code, and then carries a cookie. "Forget phones" makes new
secrets, which unpairs every phone.
"""

from __future__ import annotations

import hmac
import html
import logging
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from proofloop.app.config import Settings

log = logging.getLogger(__name__)
COOKIE = "proofloop_phone"
COOKIE_DAYS = 400
MAX_TRIES, TRIES_WINDOW_S = 5, 600  # wrong codes allowed per ten minutes, for all phones together


def lan_address() -> str:
    """This computer's address on the local network (the interface its default route uses)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # nothing is sent: this only picks the interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


PAIR_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pair this phone · ProofLoop</title>
<style>body{{font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:24px 16px;background:#f6f5f2;color:#0b0b0b}}
@media (prefers-color-scheme:dark){{body{{background:#121211;color:#f0efec}}input{{background:#1a1a19;color:inherit}}}}
main{{max-width:420px;margin:0 auto}}h1{{font-size:22px;margin:0 0 8px}}p{{margin:0 0 14px}}
input{{font:inherit;font-size:24px;letter-spacing:6px;width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #bbb;border-radius:10px}}
button{{margin-top:12px;width:100%;padding:12px;border:0;border-radius:10px;background:#2a78d6;color:#fff;font:inherit;font-weight:600}}
.err{{color:#c81e1e}}</style></head><body><main>
<h1>Pair this phone with ProofLoop</h1>
<p>Scan the QR code in ProofLoop on your computer (the <b>Phone</b> button), or type the six-digit code shown there.</p>
{error}
<form method="post" action="/pair"><label for="code">Code</label>
<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" maxlength="6" required autofocus>
<button type="submit">Pair</button></form></main></body></html>"""


class Phone:
    def __init__(self, monitor, settings: Settings):
        self.monitor = monitor
        self.settings = settings
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self._tries: list[float] = []
        self._lock = threading.Lock()
        self.app = self._build()

    # ---- state ------------------------------------------------------------------
    @property
    def on(self) -> bool:
        return bool(self.server and self.thread and self.thread.is_alive())

    def url(self) -> str:
        return f"http://{lan_address()}:{self.settings.phone_port}/"

    def pair_url(self) -> str:
        return f"{self.url()}pair?key={self.settings.phone_secret}"

    def qr_svg(self) -> str:
        import qrcode
        import qrcode.image.svg

        img = qrcode.make(self.pair_url(), image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        return img.to_string(encoding="unicode")

    def status(self, secrets: bool = False) -> dict:
        out = {"on": self.on, "url": self.url() if self.on else None, "error": self.error}
        if secrets and self.on:  # only for the ProofLoop window on this computer
            out |= {"code": self.settings.phone_code, "qr": self.qr_svg(), "pair_url": self.pair_url()}
        return out

    # ---- on / off ---------------------------------------------------------------
    def start(self) -> None:
        if self.on:
            return
        self.error = None
        server = uvicorn.Server(
            uvicorn.Config(self.app, host="0.0.0.0", port=self.settings.phone_port, log_level="warning",
                           timeout_graceful_shutdown=3)
        )
        thread = threading.Thread(target=server.run, daemon=True, name="proofloop-phone")
        thread.start()
        end = time.time() + 8
        while not server.started and thread.is_alive() and time.time() < end:
            time.sleep(0.05)
        if not server.started:
            server.should_exit = True
            self.error = (
                f"couldn't listen on port {self.settings.phone_port}: another program may be using it"
            )
            raise RuntimeError(self.error)
        self.server, self.thread = server, thread
        log.info("phone access on at %s", self.url())

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
            if self.thread:
                self.thread.join(timeout=8)
        self.server = self.thread = None

    # ---- the app phones see -------------------------------------------------------
    def _paired(self, request: Request) -> bool:
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), self.settings.phone_secret)

    def _pair_response(self) -> Response:
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(
            COOKIE,
            self.settings.phone_secret,
            max_age=COOKIE_DAYS * 86400,
            httponly=True,
            samesite="lax",  # sent when you open the link, never on another site's requests
        )
        return r

    def _too_many_tries(self) -> bool:
        now = time.time()
        with self._lock:
            self._tries = [t for t in self._tries if now - t < TRIES_WINDOW_S]
            return len(self._tries) >= MAX_TRIES

    def _build(self) -> FastAPI:
        from proofloop.app.loop_server import create_app

        api = FastAPI(title="ProofLoop phone", docs_url=None, redoc_url=None, openapi_url=None)

        @api.middleware("http")
        async def paired_only(request: Request, call_next):
            if request.url.path == "/pair" or self._paired(request):
                response = await call_next(request)
                response.headers.setdefault("Cache-Control", "no-store")
                response.headers.setdefault("X-Frame-Options", "DENY")
                return response
            if request.method == "GET" and request.url.path == "/":
                return HTMLResponse(PAIR_PAGE.format(error=""))
            return JSONResponse({"detail": "pair this phone first: open ProofLoop on your computer"}, 401)

        @api.get("/pair")
        def pair_link(key: str = ""):
            if key and hmac.compare_digest(key, self.settings.phone_secret):
                return self._pair_response()
            return HTMLResponse(PAIR_PAGE.format(error='<p class="err">That link is out of date. Scan the '
                                                       "code in ProofLoop again.</p>"), 403)

        @api.post("/pair")
        async def pair_code(request: Request):
            if self._too_many_tries():
                return HTMLResponse(PAIR_PAGE.format(error='<p class="err">Too many wrong codes. Wait ten '
                                                           "minutes, or scan the QR code instead.</p>"), 429)
            form = await request.form()
            code = str(form.get("code", "")).strip()
            if code and hmac.compare_digest(code, self.settings.phone_code):
                return self._pair_response()
            with self._lock:
                self._tries.append(time.time())
            return HTMLResponse(PAIR_PAGE.format(error=f'<p class="err">{html.escape("That code is wrong.")}'
                                                       "</p>"), 403)

        api.mount("/", create_app(self.monitor))  # the Loop files & Printers page and API, nothing else
        return api
