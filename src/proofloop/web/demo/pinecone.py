"""Second demo site, "Pinecone Home": real-world patterns with planted flaws.

Planted flaws (ground truth in pinecone_flaws.yaml):
  * newsletter pop-up that comes back on every page after you close it
  * the return policy is three levels deep (Help -> Orders & delivery -> Returns) with no direct link
  * the "Stores" menu link does nothing
Clean on purpose: the cookie banner (closes and stays closed), the sign-up form
(labels, a country dropdown, a terms checkbox), the contact page, the help search
and the phone menu (behind a three-line icon, as on most sites).
"""

from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from proofloop.web.demo.server import DemoServer

CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: "DejaVu Sans", Arial, sans-serif; font-size: 18px; color: #1c1c1c; background: #fff; }
header { background: #23472b; color: #fff; padding: 14px 20px; display: flex; flex-wrap: wrap; gap: 12px 26px; align-items: center; }
header .logo { font-weight: bold; font-size: 22px; } header a { color: #fff; text-decoration: none; }
#burger { display: none; margin-left: auto; background: none; border: 0; padding: 8px; cursor: pointer; }
#burger span { display: block; width: 24px; height: 3px; background: #fff; margin: 5px 0; }
@media (max-width: 600px) {
  #burger { display: block; }
  header nav { display: none; width: 100%; flex-direction: column; gap: 14px; padding: 8px 0 4px; }
  header.open nav { display: flex; }
}
@media (min-width: 601px) { header nav { display: contents; } }
.search { display: flex; gap: 10px; margin: 18px 0; } .search input { max-width: 320px; }
.search button { font-size: 17px; padding: 8px 16px; border: 0; border-radius: 6px; background: #2d6a39; color: #fff; }
main { max-width: 760px; margin: 0 auto; padding: 28px; }
h1 { font-size: 30px; } a.link { color: #1f5fbf; font-size: 19px; display: block; margin: 12px 0; }
label { display: block; font-weight: bold; margin: 16px 0 6px; }
input[type=text], input[type=email], input[type=password], select { width: 100%; max-width: 420px; height: 44px;
  font-size: 18px; padding: 6px 10px; border: 2px solid #666; border-radius: 4px; background: #fff; }
.check { display: flex; align-items: center; gap: 10px; margin: 18px 0; }
.check input { width: 20px; height: 20px; }
.btn { margin-top: 20px; padding: 12px 26px; font-size: 19px; border: 0; border-radius: 6px; background: #2d6a39; color: #fff; }
.ok { background: #e7f6ec; border: 2px solid #1a7f37; padding: 14px; border-radius: 6px; font-size: 22px; }
.err { background: #fde8e8; border: 2px solid #c81e1e; color: #8a1010; padding: 10px; border-radius: 6px; }
#cookie { position: fixed; left: 0; right: 0; bottom: 0; background: #222; color: #fff; padding: 18px 28px;
  display: flex; gap: 20px; align-items: center; }
#cookie button { font-size: 17px; padding: 10px 18px; border-radius: 6px; border: 0; }
#cookie .yes { background: #fff; color: #111; } #cookie .no { background: #555; color: #fff; }
#modal { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; }
#modal .box { background: #fff; padding: 30px; border-radius: 10px; width: 420px; position: relative; }
#modal .x { position: absolute; right: 14px; top: 8px; font-size: 28px; border: 0; background: none; cursor: pointer; }
#modal .thanks { margin-top: 16px; font-size: 17px; padding: 10px 16px; border: 1px solid #888; background: #fff; border-radius: 6px; }
"""

SCRIPT = """
<script>
// Cookie banner: remembers the choice (clean behaviour).
if (!document.cookie.includes('consent=')) {
  document.write('<div id="cookie"><span>We use cookies to improve your visit.</span>' +
    '<button class="yes" onclick="document.cookie=\\'consent=yes;path=/\\';this.parentNode.remove()">Accept all</button>' +
    '<button class="no" onclick="document.cookie=\\'consent=no;path=/\\';this.parentNode.remove()">Reject all</button></div>');
}
// PLANTED: the newsletter pop-up ignores that you closed it and returns on every page.
document.write('<div id="modal"><div class="box"><button class="x" onclick="document.getElementById(\\'modal\\').remove()">\\u00d7</button>' +
  '<h2>Join our newsletter</h2><p>Get 10% off your first order.</p>' +
  '<button class="thanks" onclick="document.getElementById(\\'modal\\').remove()">No thanks</button></div></div>');
</script>
"""


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>{CSS}</style></head><body>
<header><span class="logo">Pinecone Home</span>
<button id="burger" aria-label="Menu" onclick="this.parentNode.classList.toggle('open')"><span></span><span></span><span></span></button>
<nav><a href="/shop">Shop</a><a href="/help">Help</a>
<a href="#" onclick="return false">Stores</a><a href="/contact">Contact</a></nav></header>
<main>{body}</main>{SCRIPT}</body></html>""".encode()


ROUTES = {
    "/": lambda: page("Pinecone Home", "<h1>Furniture that lasts</h1><p>Solid wood, made to order.</p>"
                                       "<a class='link' href='/register'>Create an account</a>"),
    "/shop": lambda: page("Shop", "<h1>Shop</h1><p>Tables, chairs and shelves.</p>"),
    "/help": lambda: page("Help", "<h1>Help centre</h1><form class='search' action='/search'>"
                                  "<input type='text' name='q' placeholder='Search help'>"
                                  "<button type='submit'>Search</button></form>"
                                  "<a class='link' href='/help/orders'>Orders &amp; delivery</a>"
                                  "<a class='link' href='/help/account'>Your account</a>"
                                  "<a class='link' href='/help/payments'>Payments</a>"),
    "/help/orders": lambda: page("Orders", "<h1>Orders &amp; delivery</h1>"
                                           "<a class='link' href='/help/orders/tracking'>Tracking an order</a>"
                                           "<a class='link' href='/help/orders/returns'>Returns and refunds</a>"),
    "/help/orders/returns": lambda: page("Returns", "<h1>Returns and refunds</h1><p class='ok'>You have 30 days to "
                                                    "return an item for a full refund.</p>"),
    "/help/orders/tracking": lambda: page("Tracking", "<h1>Tracking</h1><p>Use the link in your email.</p>"),
    "/help/account": lambda: page("Account help", "<h1>Your account</h1><p>Reset your password from the login page.</p>"),
    "/help/payments": lambda: page("Payments", "<h1>Payments</h1><p>We accept cards and bank transfer.</p>"),
    "/contact": lambda: page("Contact", "<h1>Contact us</h1><p>Email support@pinecone.test</p>"
                                        "<p>Opening hours: Monday to Saturday, 9:00 to 18:00.</p>"),
}


SEARCH_INDEX = [  # (words, title, link) for the help search
    ({"return", "returns", "refund", "refunds", "days"}, "Returns and refunds", "/help/orders/returns"),
    ({"track", "tracking", "order", "delivery"}, "Tracking an order", "/help/orders/tracking"),
    ({"password", "account", "login"}, "Your account", "/help/account"),
    ({"pay", "payment", "payments", "card"}, "Payments", "/help/payments"),
    ({"contact", "email", "support", "hours", "phone"}, "Contact us", "/contact"),
]


def search(query: str) -> bytes:
    words = {w.strip(".,").lower() for w in query.split()}
    words |= {w[:-1] for w in words if w.endswith("s")}
    hits = [(title, link) for keys, title, link in SEARCH_INDEX if words & keys]
    body = "".join(f"<a class='link' href='{link}'>{html.escape(title)}</a>" for title, link in hits)
    return page("Search", f"<h1>Search results</h1><p>{len(hits)} result(s) for '{html.escape(query)}'</p>"
                + (body or "<p>Nothing found. Try other words.</p>"))


def register(error: str = "") -> bytes:
    banner = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    options = "".join(f"<option>{c}</option>" for c in ["France", "Germany", "Italy", "Spain", "United Kingdom"])
    return page("Create account", f"""<h1>Create your account</h1>{banner}
<form method="post" action="/register">
<label for="n">Full name</label><input id="n" name="name" type="text">
<label for="e">Email</label><input id="e" name="email" type="email">
<label for="c">Country</label><select id="c" name="country"><option value="">Select a country</option>{options}</select>
<label for="p">Password</label><input id="p" name="pw" type="password">
<div class="check"><input id="t" name="terms" type="checkbox"><label for="t" style="margin:0;font-weight:normal">I agree to the terms</label></div>
<button class="btn" type="submit">Create account</button></form>""")


class PineconeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        pass

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/register":
            return self._send(register())
        if path == "/search":
            return self._send(search(parse_qs(urlsplit(self.path).query).get("q", [""])[0]))
        if path == "/welcome":
            return self._send(page("Welcome", "<h1>Welcome to Pinecone</h1><p class='ok'>Your account is ready.</p>"))
        route = ROUTES.get(path.rstrip("/") or "/")
        self._send(route() if route else page("Not found", "<h1>Page not found</h1>"), 200 if route else 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        missing = [n for n in ("name", "email", "country", "pw") if not form.get(n)]
        if missing or "@" not in form.get("email", ""):
            return self._send(register("Please fill in: " + ", ".join(missing or ["a valid email"]) + "."))
        if form.get("terms") != "on":
            return self._send(register("Please tick 'I agree to the terms' to continue."))
        self.send_response(303)
        self.send_header("Location", "/welcome")
        self.end_headers()


class PineconeServer(DemoServer):
    def __init__(self, port: int = 8766, host: str = "127.0.0.1"):
        super().__init__(port, host, PineconeHandler)


if __name__ == "__main__":
    with PineconeServer() as s:
        print(f"Pinecone demo at {s.url} (Ctrl+C to stop)")
        try:
            while s.thread.is_alive():
                s.thread.join(0.5)
        except KeyboardInterrupt:
            pass
