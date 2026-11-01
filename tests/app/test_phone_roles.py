"""Phone access has two kinds of paired phone: watch only, and operator.

The guard is central (one middleware), so these tests walk every route registered on the phone's app
rather than a list: a route added later is checked too.
"""

import json

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

from proofloop.app import phone as phone_mod
from proofloop.app.config import Settings
from proofloop.app.loop_server import create_app
from proofloop.app.phone import OPERATOR, ROLES, WATCH, WATCH_MESSAGE, Phone
from proofloop.core.settings import Notifier, PrinterConfig
from proofloop.monitor.service import App
from tests.app.test_loop_server import FakeLink

BASE = "http://192.168.1.20:8601"
WRITES = {"POST", "PUT", "PATCH", "DELETE"}
SECRET_URL = "https://discord.com/api/webhooks/123456/very-secret-token"

# Every GET route the phone serves, looked at for side effects and secrets (see WATCH_BLOCKED_GETS in
# app/phone.py). A GET route that is not here fails ``test_every_get_route_has_been_reviewed``: look at it,
# add it to WATCH_BLOCKED_GETS if it changes anything or shows a secret, and list it here.
REVIEWED_GETS = {
    "/",  # the page
    "/js/{name}.js",  # the page's scripts: no secrets
    "/api/live",  # a stream of "something changed" events
    "/api/state",  # everything the page draws; notification addresses are cut down for watch-only phones
    "/api/queue", "/api/orders", "/api/summary", "/api/overview", "/api/events",
    "/api/dashboard",  # parts per hour, orders' parts and finish, what's next: counts only, changes nothing
    "/api/outcomes", "/api/outcomes/{oid}/file", "/api/outcomes/{oid}/picture",
    "/api/filament", "/api/filament/usage", "/api/filament/export.csv", "/api/filament/tray-colors",
    "/api/profiles/{pid}",
    "/api/monitor/{name}/snapshot",  # reads the printer's camera; changes nothing
    "/api/monitor/{name}/diff",
    "/api/monitor/{name}/camera",  # the shared latest picture; changes nothing
    "/api/monitor/{name}/live",  # the camera as video; opens the camera while watched, changes nothing
    "/api/monitor/{name}/timelapses", "/api/monitor/{name}/timelapses/{id}",
    "/api/proofs/{pid}",  # a kept picture of a finished plate, the same as the order's gallery; changes nothing
    "/api/camera-settings",
    "/api/slice/options",  # the slicer presets and your printers' nozzles and AMS trays; changes nothing
    "/api/watch-folder",  # the watch folder setting and the model files it sliced lately; changes nothing
    "/api/home-assistant",  # a phone gets "on or off" and the status, not the broker or its password
    "/api/maintenance", "/api/maintenance/log",
    "/api/orders/customers",
    "/api/orders/export.csv", "/api/outcomes/export.csv", "/api/costs/export.csv",  # the same numbers as the pages
}
# GETs with a secret or a side effect: see WATCH_BLOCKED_GETS in phone.py (the invoice gets its number on first view)
BLOCKED_FOR_WATCH = {"/api/backup", "/api/discover", "/api/orders/{oid}/invoice", "/api/business"}


def walk(app, prefix=""):
    """(method, path) of every route on an app, through its mounts."""
    for r in app.routes:
        if isinstance(r, Mount):
            yield from walk(r.app, prefix + r.path)
        elif isinstance(r, APIRoute):
            for m in sorted(r.methods or ()):
                yield m, prefix + r.path


class Farm:
    def __init__(self, tmp_path):
        self.app = App(tmp_path / "farm", connect=False, link_factory=FakeLink)
        self.app.add_printer(PrinterConfig(name="p2s", host="1.2.3.4", serial="S", access_code="x", model_id="N7"))
        self.settings = Settings()
        self.phone = Phone(self.app, self.settings)

    def client(self, role=None, **kw):
        c = TestClient(self.phone.app, base_url=BASE, **kw)
        if role:
            r = c.post("/pair", data={"code": self.phone.code(role)}, follow_redirects=False)
            assert r.status_code == 303
        return c


@pytest.fixture()
def farm(tmp_path):
    f = Farm(tmp_path)
    yield f
    f.app.shutdown()


def fill(path):
    return path.replace("{token}", "x").replace("{rid}", "x").replace("{jid}", "x")\
        .replace("{oid}", "1").replace("{uid}", "1").replace("{sid}", "x").replace("{name}", "x")\
        .replace("{pid}", "x")


def call(c, method, path):
    path = fill(path)
    if path == "/api/live":
        path += "?limit=1"
    return c.request(method, path, json={} if method in WRITES else None)


def refused_as_watch(r) -> bool:
    return r.status_code == 403 and r.json().get("detail") == WATCH_MESSAGE


# ---- the two roles pair, and the cookie names the role ---------------------------------------------
def test_each_code_and_each_qr_link_pairs_its_own_role(farm):
    s = farm.settings
    assert len({s.phone_secret, s.phone_watch_secret}) == 2 and len({s.phone_code, s.phone_watch_code}) == 2
    for role in ROLES:
        by_code = farm.client(role)
        by_link = TestClient(farm.phone.app, base_url=BASE)
        assert by_link.get(f"/pair?key={farm.phone.secret(role)}", follow_redirects=False).status_code == 303
        for c in (by_code, by_link):
            assert c.get("/api/state").json()["role"] == role
    cookie = farm.client(WATCH).post("/pair", data={"code": s.phone_watch_code}, follow_redirects=False)
    flags = cookie.headers["set-cookie"].lower()
    assert "httponly" in flags and "samesite=lax" in flags


def test_a_phone_paired_before_roles_existed_stays_paired_as_an_operator(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PROOFLOOP_HOME", str(home))
    (home / "phone.json").write_text(json.dumps({"phone": True, "phone_port": 8601, "phone_secret": "old-secret",
                                                 "phone_code": "123456"}), encoding="utf-8")
    s = Settings.load()
    assert (s.phone_secret, s.phone_code) == ("old-secret", "123456")
    assert s.phone_watch_secret not in ("", "old-secret") and s.phone_watch_code != "123456"
    assert json.loads((home / "phone.json").read_text(encoding="utf-8"))["phone_watch_secret"] == s.phone_watch_secret
    app = App(tmp_path / "farm", connect=False, link_factory=FakeLink)
    try:
        c = TestClient(Phone(app, s).app, base_url=BASE, cookies={phone_mod.COOKIE: "old-secret"})
        assert c.get("/api/state").json()["role"] == OPERATOR
        assert c.put("/api/quiet-hours", json={"enabled": False}).status_code == 200
    finally:
        app.shutdown()


def test_the_two_roles_never_share_a_secret_or_a_code():
    s = Settings(phone_secret="a", phone_watch_secret="a", phone_code="111111", phone_watch_code="111111")
    s._keep_roles_apart()
    assert s.phone_secret != s.phone_watch_secret and s.phone_code != s.phone_watch_code


def test_five_wrong_codes_lock_out_both_codes_together(farm):
    c = farm.client()
    for i in range(5):  # a mix of wrong guesses
        wrong = "000000" if i % 2 else "999999"
        if wrong in (farm.settings.phone_code, farm.settings.phone_watch_code):
            wrong = "123123"
        assert c.post("/pair", data={"code": wrong}).status_code == 403
    for role in ROLES:
        assert c.post("/pair", data={"code": farm.phone.code(role)}).status_code == 429
    assert c.get("/api/state").status_code == 401


def test_forgetting_phones_unpairs_both_roles_and_changes_every_secret(farm):
    phones = {role: farm.client(role) for role in ROLES}
    before = (farm.settings.phone_secret, farm.settings.phone_code,
              farm.settings.phone_watch_secret, farm.settings.phone_watch_code)
    farm.settings.new_phone_secrets()
    after = (farm.settings.phone_secret, farm.settings.phone_code,
             farm.settings.phone_watch_secret, farm.settings.phone_watch_code)
    assert all(a != b for a, b in zip(before, after, strict=True))
    assert len({after[0], after[2]}) == 2 and len({after[1], after[3]}) == 2
    for c in phones.values():
        assert c.get("/api/state").status_code == 401


def test_odd_cookies_keys_and_codes_are_refused_not_crashed(farm):
    c = TestClient(farm.phone.app, base_url=BASE, raise_server_exceptions=False)
    assert c.get("/api/state", headers={"cookie": f"{phone_mod.COOKIE}=é☃".encode()}).status_code == 401
    assert c.get("/pair?key=%C3%A9%E2%98%83").status_code == 403
    assert c.post("/pair", data={"code": "é☃"}).status_code == 403
    assert c.get("/api/state", headers={"cookie": f"{phone_mod.COOKIE}="}).status_code == 401


# ---- what each role may do -------------------------------------------------------------------------
def test_every_mutating_route_is_refused_for_watch_only_and_reaches_the_route_for_an_operator(farm):
    routes = [(m, p) for m, p in walk(farm.phone.app) if m in WRITES and p != "/pair"]
    assert len(routes) >= 25, routes  # the walk found the whole API, not just the pairing routes
    watch = farm.client(WATCH, raise_server_exceptions=False)
    operator = farm.client(OPERATOR, raise_server_exceptions=False)
    nobody = farm.client(raise_server_exceptions=False)
    for method, path in routes:
        assert call(nobody, method, path).status_code == 401, (method, path)
        r = call(watch, method, path)
        assert refused_as_watch(r), (method, path, r.status_code, r.text[:80])
        r = call(operator, method, path)
        assert not refused_as_watch(r) and r.status_code != 401, (method, path, r.status_code)


def test_a_watch_only_phone_changes_nothing_and_an_operator_can(farm):
    watch, operator = farm.client(WATCH), farm.client(OPERATOR)
    rates = {"currency": "EUR", "electricity_per_kwh": 0.5}
    r = watch.put("/api/costs", json=rates)
    assert refused_as_watch(r) and "only watch" in r.json()["detail"] and "operator" in r.json()["detail"]
    assert farm.app.settings.costs.currency != "EUR"
    assert watch.post("/api/filament/spools", json={"material": "PLA", "color": "#ffffff"}).status_code == 403
    assert watch.delete("/api/monitor/printers/p2s").status_code == 403 and "p2s" in farm.app.watches
    assert operator.put("/api/costs", json=rates).status_code == 200 and farm.app.settings.costs.currency == "EUR"
    assert operator.post("/api/filament/spools", json={"material": "PLA", "color": "#ffffff"}).status_code == 200
    assert len(farm.app.inventory.all()) == 1


def test_a_watch_only_phone_sees_what_an_operator_sees(farm):
    watch, operator = farm.client(WATCH), farm.client(OPERATOR)
    for path in ("/", "/api/state", "/api/filament", "/api/overview", "/api/queue", "/api/orders", "/api/summary",
                 "/api/events", "/api/outcomes", "/api/live?limit=1", "/js/core.js"):
        a, b = watch.get(path), operator.get(path)
        assert a.status_code == b.status_code == 200, (path, a.status_code, b.status_code)
    assert watch.get("/js/core.js").text == operator.get("/js/core.js").text


def test_a_watch_only_phone_cant_download_the_backup_or_scan_the_network(farm):
    watch, operator = farm.client(WATCH), farm.client(OPERATOR)
    for path in ("/api/backup", "/api/discover", "/api/orders/x/invoice", "/api/orders/x/invoice/"):
        assert refused_as_watch(watch.get(path)), path
    for sneaky in ("/api/backup/", "//api/backup", "/api//backup", "/api/backup//"):
        r = watch.get(sneaky, follow_redirects=False)
        assert r.status_code != 200 and r.headers.get("content-type") != "application/zip", sneaky
    assert watch.get("/api/backup?x=1").status_code == 403
    assert refused_as_watch(watch.get("/api/business"))  # the invoice sender's address and payment details
    r = operator.get("/api/backup")  # no phone gets the printers' access codes
    assert r.status_code == 403 and r.headers.get("content-type") != "application/zip"


def test_a_phone_is_told_its_role_and_gets_no_webhook_address(farm):
    farm.app.settings.notifications = [Notifier(kind="discord", url=SECRET_URL, token="tok")]
    watch, operator = farm.client(WATCH), farm.client(OPERATOR)
    w, o = watch.get("/api/state").json(), operator.get("/api/state").json()
    assert (w["role"], o["role"]) == (WATCH, OPERATOR) and not w["can_send"] and not o["can_send"]
    assert w["notifications"][0]["url"] == "https://discord.com/…" and "token" not in w["notifications"][0]
    assert "very-secret-token" not in watch.get("/api/state").text
    assert "very-secret-token" not in operator.get("/api/state").text  # no phone gets the address
    assert TestClient(create_app(farm.app)).get("/api/state").json()["role"] == "window"


def test_an_operator_still_never_sends_a_print_or_changes_the_window_only_settings(farm):
    operator = farm.client(OPERATOR)
    assert operator.put("/api/send-to-printer", json={"on": True}).status_code == 403
    assert operator.put("/api/failure-watch", json={"mode": "pause"}).status_code == 403
    assert operator.post("/api/plates/x/send", json={"printer": "p2s", "confirm": "p2s"}).status_code == 403
    assert operator.post("/api/queue/x/y/send", json={"confirm": "p2s"}).status_code in (403, 404)
    assert operator.post("/api/restore", files={"file": ("b.zip", b"zip")}).status_code == 403
    assert not farm.app.settings.send_to_printer and farm.app.settings.failure_watch == "alert"


def test_unpaired_requests_get_401_and_the_pairing_page(farm):
    c = farm.client(raise_server_exceptions=False)
    assert "Pair this phone" in c.get("/").text
    for method, path in walk(farm.phone.app):
        if path in ("/", "/pair") and method in ("GET", "POST"):
            continue
        assert call(c, method, path).status_code == 401, (method, path)
    assert c.get("/api/state", headers={"cookie": f"{phone_mod.COOKIE}=guess"}).status_code == 401


# ---- every GET has been looked at ------------------------------------------------------------------
def test_every_get_route_has_been_reviewed(farm):
    gets = {p for m, p in walk(farm.phone.app) if m == "GET" and p != "/pair"}
    assert BLOCKED_FOR_WATCH <= gets and phone_mod.WATCH_BLOCKED_GETS == BLOCKED_FOR_WATCH
    new = gets - REVIEWED_GETS - BLOCKED_FOR_WATCH
    assert not new, (
        f"new GET route(s) {sorted(new)}: a watch-only phone can call these. Check that each changes nothing and "
        "shows no secret (an access code, a webhook address, a token). If one does, add its path to "
        "WATCH_BLOCKED_GETS in app/phone.py and BLOCKED_FOR_WATCH here; then list it in REVIEWED_GETS."
    )
    gone = REVIEWED_GETS - gets
    assert not gone, f"REVIEWED_GETS lists routes that no longer exist: {sorted(gone)}"


def test_the_watch_guard_only_looks_at_the_method_and_the_path():
    assert phone_mod.watch_refusal("GET", "/api/state") is None
    assert phone_mod.watch_refusal("HEAD", "/api/state") is None
    for m in ("POST", "PUT", "PATCH", "DELETE", "TRACE", "get"):
        assert phone_mod.watch_refusal(m, "/api/state") == WATCH_MESSAGE
    assert phone_mod.watch_refusal("GET", "/api/backup") == WATCH_MESSAGE
    assert phone_mod.watch_refusal("GET", "/api//backup/") == WATCH_MESSAGE


# ---- CSRF, framing, web sockets --------------------------------------------------------------------
def test_a_change_asked_for_by_another_page_is_refused(farm):
    operator = farm.client(OPERATOR)
    body = {"enabled": False}
    assert operator.put("/api/quiet-hours", json=body, headers={"Origin": BASE}).status_code == 200
    assert operator.put("/api/quiet-hours", json=body).status_code == 200  # no Origin: not a browser page
    for origin in ("http://192.168.1.20:9999", "http://evil.example", "null"):
        r = operator.put("/api/quiet-hours", json=body, headers={"Origin": origin})
        assert r.status_code == 403 and "this page" in r.json()["detail"], origin
    assert operator.get("/api/state", headers={"Origin": "http://evil.example"}).status_code == 200  # reads: cookies are SameSite=Lax


def test_every_answer_cant_be_framed_and_isnt_cached(farm):
    c = farm.client(WATCH)
    unpaired = farm.client()
    answers = [unpaired.get("/"), unpaired.get("/api/state"), unpaired.get("/pair?key=no"), unpaired.post("/pair", data={"code": "x"}),
               c.get("/"), c.get("/api/state"), c.put("/api/costs", json={}), c.get("/api/backup")]
    for r in answers:
        assert r.headers["x-frame-options"] == "DENY" and "no-store" in r.headers["cache-control"], r.request.url


def test_web_sockets_are_refused(farm):
    c = farm.client(OPERATOR)
    with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws"):
        pass


# ---- what the window shows, and the command line ------------------------------------------------------
def test_the_window_gets_both_codes_and_both_qr_codes(farm, monkeypatch):
    monkeypatch.setattr(Phone, "on", property(lambda self: True))
    st = farm.phone.status(secrets=True)
    roles = {r["role"]: r for r in st["roles"]}
    assert list(roles) == [WATCH, OPERATOR]
    assert roles[WATCH]["code"] == farm.settings.phone_watch_code and roles[OPERATOR]["code"] == farm.settings.phone_code
    assert roles[WATCH]["pair_url"].endswith(farm.settings.phone_watch_secret)
    assert roles[OPERATOR]["pair_url"].endswith(farm.settings.phone_secret)
    assert "<svg" in roles[WATCH]["qr"] and "<svg" in roles[OPERATOR]["qr"] and roles[WATCH]["qr"] != roles[OPERATOR]["qr"]
    assert (st["code"], st["pair_url"]) == (roles[OPERATOR]["code"], roles[OPERATOR]["pair_url"])  # as before roles
    assert "roles" not in farm.phone.status() and "code" not in farm.phone.status()  # nothing secret without asking


def test_proofloop_phone_prints_both_codes_or_one_role_or_json(farm, monkeypatch, capsys):
    from proofloop import cli

    monkeypatch.setattr(Phone, "on", property(lambda self: True))
    status = farm.phone.status(secrets=True)
    monkeypatch.setattr(cli, "_ask", lambda *a, **k: {"phone": status})
    s = farm.settings
    assert cli.main(["phone", "on"]) == 0
    out = capsys.readouterr().out
    assert s.phone_code in out and s.phone_watch_code in out and "Watch only" in out and "Operator" in out
    assert cli.main(["phone", "on", "--role", "watch"]) == 0
    out = capsys.readouterr().out
    assert s.phone_watch_code in out and s.phone_code not in out
    assert cli.main(["phone", "status", "--json", "--role", "operator"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["on"] and [r["role"] for r in d["roles"]] == [OPERATOR] and d["roles"][0]["code"] == s.phone_code
    assert "qr" not in d["roles"][0] and d["roles"][0]["pair_url"].endswith(s.phone_secret)
    assert cli.main(["phone", "on", "--json"]) == 0
    assert [r["role"] for r in json.loads(capsys.readouterr().out)["roles"]] == [WATCH, OPERATOR]
    monkeypatch.setattr(cli, "_ask", lambda *a, **k: {"phone": {"on": False, "error": None}})
    assert cli.main(["phone", "off", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"on": False, "url": None, "error": None, "roles": []}
    monkeypatch.setattr(cli, "_ask", lambda *a, **k: None)
    assert cli.main(["phone", "on", "--json"]) == 2
    assert "isn't running" in json.loads(capsys.readouterr().out)["error"]



def test_wrong_codes_sent_all_at_once_still_count_against_the_limit(farm):
    """Each typed code is counted before it is compared, so a burst can't get more than five guesses."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=20) as pool:
        stamps = list(pool.map(lambda _: farm.phone._take_try(), range(50)))
    assert sum(s is not None for s in stamps) == phone_mod.MAX_TRIES
    c = farm.client()
    assert c.post("/pair", data={"code": farm.phone.code(OPERATOR)}).status_code == 429  # even the right one


def test_a_right_code_does_not_use_up_a_try(farm):
    for _ in range(phone_mod.MAX_TRIES + 2):
        farm.client(OPERATOR)
    assert farm.client().post("/pair", data={"code": "not-a-code"}).status_code == 403  # not locked out


def test_an_operator_phone_cant_change_how_often_the_failure_watch_looks(farm):
    op = farm.client(OPERATOR)
    assert op.put("/api/camera-settings", json={"look_every_pct": 5}).status_code == 403
    assert op.get("/api/camera-settings").status_code == 200


def test_a_phone_gets_how_the_home_assistant_export_is_going_but_not_its_errors(farm):
    farm.app.homeassistant.error = "can't reach the broker at 10.0.0.9:1883"
    body = farm.client(OPERATOR).get("/api/home-assistant").json()
    assert "10.0.0.9" not in json.dumps(body) and set(body["status"]) == {"state"}
