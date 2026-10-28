"""Small demo shop with deliberately planted UX flaws (and clean control pages).

Run:  python demo_app/server.py [--port 8765]

Planted flaws (ground truth in planted_flaws.yaml):
  /signup    confusing form: ambiguous "ID" label, date format never explained,
             vague "Invalid input." error, every field cleared on error
  /plans     hidden button: "Continue" is light grey on white, far below the fold
  /shipping  slow page: the server waits 6 seconds, nothing shows progress
  /checkout  unexplained error: the first "Place order" shows only "Error 0x3F"
Clean controls: /login -> /account, / -> /contact
"""

from __future__ import annotations

import argparse
import html
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SLOW_SECONDS = 6.0
TEST_EMAIL = "tester@example.test"
TEST_PASSWORD = "demo-pass"

BASE_CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: "DejaVu Sans", Arial, sans-serif; font-size: 18px; color: #1d1d1d; background: #ffffff; }
header { background: #1f3a5f; color: #fff; padding: 16px 28px; display: flex; align-items: center; gap: 28px; }
header .brand-logo { font-weight: bold; font-size: 22px; }
header a { color: #fff; text-decoration: none; font-size: 18px; }
main { max-width: 760px; margin: 0 auto; padding: 28px; }
h1 { font-size: 30px; margin: 8px 0 20px; }
label { display: block; margin: 18px 0 6px; font-weight: bold; }
input[type=text], input[type=email], input[type=password] {
  width: 100%; max-width: 420px; height: 44px; font-size: 18px; padding: 6px 10px;
  border: 2px solid #6b6b6b; border-radius: 4px; background: #fff; }
.btn { display: inline-block; margin-top: 24px; padding: 12px 26px; font-size: 19px; border: 0; border-radius: 6px;
  background: #1f6feb; color: #fff; text-decoration: none; cursor: pointer; }
.hint { color: #444; font-size: 16px; margin-top: 4px; }
.error-banner { background: #fde8e8; border: 2px solid #c81e1e; color: #8a1010; padding: 12px 16px; border-radius: 6px; }
.ok { background: #e7f6ec; border: 2px solid #1a7f37; color: #0f5223; padding: 14px 18px; border-radius: 6px; font-size: 22px; }
.card { border: 2px solid #c9c9c9; border-radius: 8px; padding: 16px 20px; margin: 14px 0; cursor: pointer; }
.card.selected { border-color: #1f6feb; background: #eef4ff; }
.card h2 { margin: 0 0 6px; font-size: 22px; }
.features li { margin: 10px 0; color: #333; }
.ghost-continue { color: #d3d3d3; background: #ffffff; border: 0; font-size: 15px; padding: 4px 8px; cursor: pointer; }
"""


def page(title: str, body: str, extra_head: str = "") -> bytes:
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>{BASE_CSS}</style>{extra_head}</head><body>
<header><span class="brand-logo">Northwind Goods</span><a href="/">Home</a><a href="/plans">Plans</a>
<a href="/cart">Cart</a><a href="/login">Log in</a><a href="/contact">Contact</a></header>
<main>{body}</main></body></html>"""
    return doc.encode("utf-8")


# ----------------------------------------------------------------------- pages

def home() -> bytes:
    return page("Northwind Goods", """
<h1>Welcome to Northwind Goods</h1>
<p>Outdoor gear for every season. Browse our plans or get in touch.</p>
<p><a class="btn" href="/contact">Contact us</a></p>
<p class="hint">Free returns within 30 days.</p>""")


def contact() -> bytes:
    return page("Contact", """
<h1>Contact us</h1>
<p>Email us at help@northwind.test and we reply within one business day.</p>
<p>Phone support: weekdays 9:00 to 17:00.</p>""")


def login(error: str = "") -> bytes:
    banner = f'<p class="error-banner">{html.escape(error)}</p>' if error else ""
    return page("Log in", f"""
<h1>Log in</h1>{banner}
<form method="post" action="/login">
<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="off">
<label for="password">Password</label><input id="password" name="password" type="password">
<div><button class="btn" type="submit">Log in</button></div>
</form>""")


def account() -> bytes:
    return page("Your account", """
<h1>Your account</h1><p class="ok">Welcome back! You are logged in.</p>
<p>Recent orders: none yet.</p>""")


def signup(error: bool = False) -> bytes:
    # Planted flaw: vague error and every field is cleared when anything is wrong.
    banner = '<p class="error-banner">Invalid input.</p>' if error else ""
    return page("Create account", f"""
<h1>Create account</h1>{banner}
<form method="post" action="/signup">
<label for="fullname">Full name</label><input id="fullname" name="fullname" type="text" autocomplete="off">
<label for="ident">ID</label><input id="ident" name="ident" type="text" autocomplete="off">
<label for="email">Email</label><input id="email" name="email" type="text" autocomplete="off">
<label for="dob">DOB</label><input id="dob" name="dob" type="text" autocomplete="off">
<label for="pw">Password</label><input id="pw" name="pw" type="password">
<div><button class="btn" type="submit">Create account</button></div>
</form>""")


def signup_done() -> bytes:
    return page("Account created", '<h1>Welcome!</h1><p class="ok">Account created. You can now log in.</p>')


def plans() -> bytes:
    features = "".join(f"<li>{f}</li>" for f in [
        "Free standard shipping on every order", "Member-only seasonal sales", "Extended 60-day returns",
        "Priority phone support", "Gear repair discounts", "Early access to new collections",
        "Monthly trail guide newsletter", "Carbon-neutral delivery", "Gift wrapping on request",
        "Birthday voucher", "Rental discounts on camping gear", "Workshops and group hikes"])
    # Planted flaw: the only way forward is a light-grey button far below the fold.
    return page("Choose a plan", f"""
<h1>Choose your membership plan</h1>
<div class="card" data-plan="Basic"><h2>Basic</h2><p>Free. Order tracking and newsletters.</p></div>
<div class="card" data-plan="Plus"><h2>Plus</h2><p>$5 per month. Free shipping and longer returns.</p></div>
<div class="card" data-plan="Pro"><h2>Pro</h2><p>$12 per month. Everything, plus repairs and rentals.</p></div>
<h2>What every member gets</h2><ul class="features">{features}</ul>
<p class="hint">Plans renew monthly. You can change plans at any time from your account page.</p>
<div style="height: 380px"></div>
<form method="get" action="/plans/done" style="text-align:right">
<input type="hidden" name="plan" id="plan" value="Basic">
<button class="ghost-continue" type="submit">Continue</button></form>
<script>
document.querySelectorAll('.card').forEach(c => c.addEventListener('click', () => {{
  document.querySelectorAll('.card').forEach(x => x.classList.remove('selected'));
  c.classList.add('selected'); document.getElementById('plan').value = c.dataset.plan; }}));
</script>""")


def plans_done(plan: str) -> bytes:
    return page("Plan selected", f'<h1>Thanks!</h1><p class="ok">Plan selected: {html.escape(plan)}.</p>')


def cart() -> bytes:
    return page("Your cart", """
<h1>Your cart</h1>
<p>1 x Trail Backpack 30L ......... $89.00</p>
<p>1 x Insulated Bottle ............ $24.00</p>
<p><strong>Subtotal: $113.00</strong></p>
<p><a class="btn" href="/shipping">Go to shipping</a></p>""")


def shipping() -> bytes:
    return page("Shipping", """
<h1>Shipping options</h1>
<p>Standard (3-5 days): free</p><p>Express (1-2 days): $9.00</p>
<p><a class="btn" href="/checkout">Continue to checkout</a></p>""")


def checkout(error: bool = False, attempt: int = 0) -> bytes:
    # Planted flaw: the first attempt fails with a code and no explanation or next step.
    banner = '<p class="error-banner">Error 0x3F</p>' if error else ""
    return page("Checkout", f"""
<h1>Review and pay</h1>{banner}
<p>Trail Backpack 30L and Insulated Bottle</p>
<p><strong>Total: $113.00</strong> (test card on file ending 4242)</p>
<form method="post" action="/checkout"><input type="hidden" name="attempt" value="{attempt}">
<button class="btn" type="submit">Place order</button></form>""")


def order_done() -> bytes:
    return page("Order placed", '<h1>Thank you!</h1><p class="ok">Order placed. A receipt is on its way.</p>')


# --------------------------------------------------------------------- handler

DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "NorthwindDemo/1.0"

    def log_message(self, fmt: str, *args) -> None:  # keep test output quiet
        pass

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        data = parse_qs(self.rfile.read(length).decode("utf-8"))
        return {k: v[0] for k, v in data.items()}

    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        routes = {
            "/": home, "/contact": contact, "/login": login, "/account": account, "/signup": signup,
            "/signup/done": signup_done, "/plans": plans, "/cart": cart, "/checkout": checkout,
            "/order/done": order_done,
        }
        if url.path == "/shipping":
            time.sleep(SLOW_SECONDS)  # planted flaw: slow page with no feedback
            return self._send(shipping())
        if url.path == "/plans/done":
            return self._send(plans_done(query.get("plan", "Basic")))
        if url.path in routes:
            return self._send(routes[url.path]())
        self._send(page("Not found", "<h1>Page not found</h1>"), 404)

    def do_POST(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        form = self._form()
        if url.path == "/login":
            if form.get("email", "").strip().lower() == TEST_EMAIL and form.get("password") == TEST_PASSWORD:
                return self._redirect("/account")
            return self._send(login("That email or password is incorrect. Check for typos and try again."))
        if url.path == "/signup":
            ok = (len(form.get("fullname", "").strip()) >= 2 and len(form.get("ident", "").strip()) >= 3
                  and "@" in form.get("email", "") and DATE_ISO.match(form.get("dob", "").strip() or "")
                  and len(form.get("pw", "")) >= 6)
            return self._redirect("/signup/done") if ok else self._send(signup(error=True))
        if url.path == "/checkout":
            attempt = int(form.get("attempt", "0") or 0)
            if attempt == 0:
                return self._send(checkout(error=True, attempt=1))
            return self._redirect("/order/done")
        self._send(page("Not found", "<h1>Page not found</h1>"), 404)


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:  # browsers abort slow requests; that's fine
        import sys

        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class DemoServer:
    """Run the demo app in a background thread (used by tests)."""

    def __init__(self, port: int = 8765, host: str = "127.0.0.1", handler: type | None = None):
        self.httpd = _QuietServer((host, port), handler or DemoHandler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "DemoServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Northwind demo shop with planted UX flaws")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with DemoServer(args.port) as server:
        print(f"demo app running at {server.url} (Ctrl+C to stop)")
        try:
            while server.thread.is_alive():  # join with a timeout so Ctrl+C works on Windows
                server.thread.join(0.5)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
