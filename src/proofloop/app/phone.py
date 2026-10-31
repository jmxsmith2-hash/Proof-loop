"""Phone access: Loop files & Printers (printer status, loop files, filament, costs) on your phone.

A second listener on your local network serves the Loop files & Printers page and API only: never
Check, never the window's handoffs or settings. It shares the one running printer monitor, so the
printers are still watched once. A phone pairs once, by scanning the QR code in the ProofLoop window
(a link holding a long secret) or by typing the six-digit code, and then carries a cookie.

A phone pairs in one of two roles, each with its own secret and code: **watch only** (sees everything a
phone sees, changes nothing) and **operator** (changes what the page lets a phone change; never sends a
print or touches the window-only settings). The cookie holds its role's secret, so it names the role.
The check is central, in one middleware, so routes added later are covered without touching them: a
watch-only phone's request is refused with 403 unless it is a GET, HEAD or OPTIONS that is not in
``WATCH_BLOCKED_GETS``. ``tests/app/test_phone_roles.py`` fails for a GET route nobody has reviewed
yet. "Forget phones" makes new secrets and codes for both roles, which unpairs every phone.
"""

from __future__ import annotations

import hmac
import html
import logging
import re
import socket
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from proofloop.app.config import Settings
from proofloop.core import theme

log = logging.getLogger(__name__)
COOKIE = "proofloop_phone"
COOKIE_DAYS = 400
MAX_TRIES, TRIES_WINDOW_S = 5, 600  # wrong codes allowed per ten minutes, for all phones and both codes together

WATCH, OPERATOR = "watch", "operator"
ROLES = (WATCH, OPERATOR)
LABELS = {WATCH: "Watch only", OPERATOR: "Operator"}
WATCH_MESSAGE = "this phone can only watch; pair it as an operator in ProofLoop to change things"
READ_ONLY = frozenset({"GET", "HEAD", "OPTIONS"})
# GETs a watch-only phone may not make, written as the routes are (``{oid}`` is any one path part): the
# backup holds the whole farm, printer access codes included; discovery sends broadcast packets on your
# network and takes seconds; an invoice's first look gives it its number and saves the settings.
WATCH_BLOCKED_GETS = frozenset({"/api/backup", "/api/discover", "/api/orders/{oid}/invoice", "/api/business"})


def _route_pattern(route: str) -> str:
    return "".join("[^/]+" if part.startswith("{") else re.escape(part) for part in re.split(r"(\{[^/}]*\})", route))


_BLOCKED = re.compile("^(?:" + "|".join(_route_pattern(g) for g in sorted(WATCH_BLOCKED_GETS)) + ")$")


def tidy(path: str) -> str:
    """A path as the guard compares it: one slash between parts, none at the end."""
    return re.sub(r"/+", "/", path).rstrip("/") or "/"


def same(a: str, b: str) -> bool:
    """Constant-time equality for any text (``compare_digest`` on str refuses non-ASCII)."""
    return hmac.compare_digest(a.encode(), b.encode())


def watch_refusal(method: str, path: str) -> str | None:
    """Why a watch-only phone may not make this request, or None."""
    if method not in READ_ONLY or _BLOCKED.match(tidy(path)):
        return WATCH_MESSAGE
    return None


class _NoWebSockets:
    """Phones have no use for web sockets, and the HTTP middleware can't see them: refuse them all."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


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
<link rel="icon" href="__PROOFLOOP_FAVICON__"><style>/*__THEME__*/</style>
<style>main{{max-width:420px;padding-top:28px}}p{{margin:0 0 14px}}.brand{{margin-bottom:18px}}
input#code{{font-size:24px;letter-spacing:6px;padding:10px 12px;border-radius:10px;margin:4px 0 12px}}
button.btn{{width:100%;justify-content:center}}.err{{color:var(--bad)}}</style></head><body><main>
<div class="brand">__PROOFLOOP_ICON__ProofLoop</div>
<h1>Pair this phone</h1>
<p class="sub">Scan the QR code in ProofLoop on your computer (More › <b>Phone</b>), or type the six-digit code shown there. The one you use decides whether this phone can only watch or can change things too.</p>
{error}
<form method="post" action="/pair"><label class="field" for="code"><span>Code</span></label>
<input id="code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" maxlength="6"
 required autofocus>
<button type="submit" class="btn primary big">Pair</button></form></main></body></html>"""


def pair_page(error: str = "") -> str:
    return theme.inline(PAIR_PAGE.format(error=error))


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

    def secret(self, role: str) -> str:
        return self.settings.phone_watch_secret if role == WATCH else self.settings.phone_secret

    def code(self, role: str) -> str:
        return self.settings.phone_watch_code if role == WATCH else self.settings.phone_code

    def pair_url(self, role: str = OPERATOR) -> str:
        return f"{self.url()}pair?key={self.secret(role)}"

    def qr_svg(self, role: str = OPERATOR) -> str:
        import qrcode
        import qrcode.image.svg

        img = qrcode.make(self.pair_url(role), image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        return img.to_string(encoding="unicode")

    def status(self, secrets: bool = False) -> dict:
        out: dict = {"on": self.on, "url": self.url() if self.on else None, "error": self.error}
        if secrets and self.on:  # only for the ProofLoop window on this computer
            roles = [{"role": r, "label": LABELS[r], "code": self.code(r), "qr": self.qr_svg(r),
                      "pair_url": self.pair_url(r)} for r in ROLES]
            operator = roles[ROLES.index(OPERATOR)]  # the top-level keys are the operator's, as before roles
            out |= {"roles": roles, "code": operator["code"], "qr": operator["qr"], "pair_url": operator["pair_url"]}
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
    def _match(self, given: str, of: Callable[[str], str]) -> str | None:
        """The role whose secret (or code) this is. Both are always compared: the time says nothing."""
        found = None
        for role in ROLES:
            if same(given, of(role)):
                found = role
        return found

    def role_of(self, request: Request) -> str | None:
        """The role this phone was paired as, or None (not paired, or forgotten since)."""
        return self._match(request.cookies.get(COOKIE, ""), self.secret)

    def _pair_response(self, role: str) -> Response:
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(
            COOKIE,
            self.secret(role),
            max_age=COOKIE_DAYS * 86400,
            httponly=True,
            samesite="lax",  # sent when you open the link, never on another site's requests
        )
        return r

    def _take_try(self) -> float | None:
        """Count a typed code before it is compared, so codes sent all at once can't slip past the limit.
        Returns its stamp (given back on a right code with ``_give_back``), or None after too many."""
        now = time.time()
        with self._lock:
            self._tries = [t for t in self._tries if now - t < TRIES_WINDOW_S]
            if len(self._tries) >= MAX_TRIES:
                return None
            self._tries.append(now)
            return now

    def _give_back(self, stamp: float) -> None:
        with self._lock:
            if stamp in self._tries:
                self._tries.remove(stamp)

    def _build(self) -> FastAPI:
        from proofloop.app.loop_server import create_app

        api = FastAPI(title="ProofLoop phone", docs_url=None, redoc_url=None, openapi_url=None)
        api.add_middleware(_NoWebSockets)

        def sealed(response: Response) -> Response:
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("X-Frame-Options", "DENY")
            return response

        def refuse(detail: str, status: int) -> Response:
            return sealed(JSONResponse({"detail": detail}, status))

        @api.middleware("http")
        async def paired_only(request: Request, call_next):
            """Every request passes here. Unpaired: the pairing page, or 401. Watch only: reads only. Nobody
            changes anything from another site's page. Routes added later are covered without a change."""
            path = request.scope["path"]
            if path == "/pair":
                return sealed(await call_next(request))
            role = self.role_of(request)
            if role is None:
                if request.method == "GET" and path == "/":
                    return sealed(HTMLResponse(pair_page()))
                return refuse("pair this phone first: open ProofLoop on your computer", 401)
            origin = request.headers.get("origin")
            if request.method not in READ_ONLY and origin is not None \
                    and urlsplit(origin).netloc != request.headers.get("host"):
                # SameSite=Lax can't tell two pages on one address apart: another port is the same site
                return refuse("requests must come from this page", 403)
            if role == WATCH and (why := watch_refusal(request.method, path)):
                return refuse(why, 403)
            request.state.role = role  # /api/state tells the page which, so it can hide what won't work
            return sealed(await call_next(request))

        @api.get("/pair")
        def pair_link(key: str = ""):
            role = self._match(key, self.secret) if key else None
            if role:
                return self._pair_response(role)
            return HTMLResponse(pair_page('<p class="err">That link is out of date. Scan the '
                                                       "code in ProofLoop again.</p>"), 403)

        @api.post("/pair")
        async def pair_code(request: Request):
            form = await request.form()
            stamp = self._take_try()
            if stamp is None:
                return HTMLResponse(pair_page('<p class="err">Too many wrong codes. Wait ten '
                                                           "minutes, or scan the QR code instead.</p>"), 429)
            code = str(form.get("code", "")).strip()
            role = self._match(code, self.code) if code else None
            if role:
                self._give_back(stamp)
                return self._pair_response(role)
            return HTMLResponse(pair_page(f'<p class="err">{html.escape("That code is wrong.")}'
                                                       "</p>"), 403)

        api.mount("/", create_app(self.monitor, allow_send=False))  # the page and API, never sending prints
        return api
