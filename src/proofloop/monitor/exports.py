"""Spreadsheet exports (orders, print history, production and costs) and the data of an invoice.

The CSV is made for Excel and Sheets: UTF-8 with a byte-order mark, dates as ``2026-10-30`` (times as
``2026-10-30 14:05:00``, your local time), numbers with no currency symbol (the currency has a column of
its own), and a blank where ProofLoop doesn't know a value. Text that comes from people (customer names,
notes, file names) is never run as a formula: a cell starting with ``= + - @`` gets a ``'`` in front.
"""

from __future__ import annotations

import csv
import io
import math
import time
from datetime import date, datetime, timedelta
from typing import Any

from proofloop.monitor.journal import CANCELLED_ERROR
from proofloop.monitor.orders import order_value, progress
from proofloop.monitor.tracking import job_key

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n")
MAX_ROWS = 1_000_000
BY = ("order", "day", "month")

ORDER_COLUMNS = ["order", "customer", "file", "parts", "made", "ordered", "due", "status", "ready_by", "on_time",
                 "price_each", "value", "earned", "cost", "profit", "currency", "paid", "paid_on", "invoice", "notes"]
HISTORY_COLUMNS = ["printer", "file", "loop", "started", "ended", "result", "layer", "total_layers", "reason",
                   "plate_check"]
COST_COLUMNS = {
    "order": ["order", "customer", "file", "loops", "failed_loops", "parts", "grams", "print_hours", "cost", "price",
              "profit", "margin_pct", "currency"],
    "period": ["period", "loops", "failed_loops", "parts", "grams", "print_hours", "order_parts", "cost", "price",
               "profit", "margin_pct", "currency"],
}


# ---- the CSV itself ------------------------------------------------------------------------
def safe(v: Any) -> Any:
    """A cell that a spreadsheet can't mistake for a formula."""
    if isinstance(v, str) and v.startswith(FORMULA_STARTS):
        return "'" + v
    return v


def to_csv(columns: list[str], rows: list[dict[str, Any]]) -> bytes:
    """The rows as CSV bytes: UTF-8 with a BOM, so Excel reads accents and symbols right."""
    return csv_text(columns, rows).encode("utf-8-sig")


def csv_text(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([safe(r.get(c)) for c in columns])
    return buf.getvalue()


def stamp(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


def day(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


def yes_no(v: bool | None) -> str:
    return "" if v is None else "yes" if v else "no"


def money(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


# ---- a date range --------------------------------------------------------------------------
def date_range(start: str | None, end: str | None) -> tuple[float, float]:
    """(from, until) as timestamps for two ``YYYY-MM-DD`` days, both included; no day means no limit."""
    try:
        first = date.fromisoformat(start) if start else None
        last = date.fromisoformat(end) if end else None
    except ValueError as e:
        raise ValueError("give the dates as YYYY-MM-DD") from e
    if first and last and first > last:
        raise ValueError("the first date is after the last one")
    since = datetime(first.year, first.month, first.day).timestamp() if first else 0.0
    until = datetime.combine(last + timedelta(days=1), datetime.min.time()).timestamp() if last else math.inf
    return since, until


# ---- orders --------------------------------------------------------------------------------
def orders_rows(app, since: float = 0.0, until: float = math.inf) -> list[dict[str, Any]]:
    """One row per order taken in the range, oldest first."""
    cur = app.settings.costs.currency
    out = []
    for o in sorted(app.orders.all(), key=lambda o: o["created"]):
        if not since <= o["created"] < until:
            continue
        p = progress(app, o)
        value, cancelled = order_value(o), o["status"] == "cancelled"
        earned = None if value is None else (value if o["status"] == "done" else 0.0)
        out.append({
            "order": o["id"], "customer": o["customer"], "file": o["plate"], "parts": o["quantity"],
            "made": p["made"], "ordered": day(o["created"]), "due": o.get("due") or "", "status": o["status"],
            "ready_by": "" if p["complete"] or cancelled else stamp(p["eta"]), "on_time": yes_no(None if cancelled else p["on_time"]),
            "price_each": o.get("price_each"), "value": None if cancelled else value,
            "earned": earned,
            "cost": None if cancelled else p["money"].get("cost"),
            "profit": None if cancelled else p["money"].get("profit"),
            "currency": cur, "paid": yes_no(bool(o.get("paid"))), "paid_on": day(o.get("paid_at")),
            "invoice": (o.get("invoice") or {}).get("label", ""), "notes": o.get("notes") or "",
        })
    return out


# ---- print history -------------------------------------------------------------------------
def reason(o: dict[str, Any]) -> str:
    """Why a print stopped, from what the printer said: its error and HMS alerts."""
    bits = []
    err = o.get("print_error")
    if err:
        bits.append("stopped at the printer" if str(err).upper() == f"{CANCELLED_ERROR:08X}" else f"error {err}")
    codes = [h.get("code") for h in o.get("hms") or [] if h.get("code")]
    if codes:
        bits.append("HMS " + ", ".join(codes))
    return "; ".join(bits)


def history_rows(app, since: float = 0.0, until: float = math.inf) -> list[dict[str, Any]]:
    """One row per print that ended in the range, oldest first."""
    rows = [o for o in app.store.outcomes(since=since, limit=MAX_ROWS) if o["ts"] < until]
    plate = {"clear": "empty", "parts_left": "parts left"}
    return [{
        "printer": o["printer"], "file": o.get("job") or "", "loop": o.get("loop_no") if o.get("loop_file") else "",
        "started": stamp(o.get("started")), "ended": stamp(o.get("ended") or o["ts"]), "result": o["outcome"],
        "layer": o.get("layer") or "", "total_layers": o.get("total_layers") or "", "reason": reason(o),
        "plate_check": plate.get(o.get("eject") or "", ""),
    } for o in sorted(rows, key=lambda o: o["ts"])]


# ---- production and costs -------------------------------------------------------------------
class _Sum:
    """Loops, parts, grams, hours and money for one order or one period."""

    def __init__(self) -> None:
        self.loops = self.failed = self.parts = self.order_parts = self.costed = self.priced = 0
        self.grams = self.hours = self.cost = self.price = 0.0

    def add_loop(self, r: dict[str, Any], order: dict[str, Any] | None) -> None:
        if not r["ok"]:
            self.failed += 1
            return
        self.loops += 1
        self.parts += r["parts"]
        self.grams += r["grams"]
        if order is None:
            return
        self.order_parts += r["parts"]
        if order.get("cost_each") is not None:
            self.costed += r["parts"]
            self.cost += r["parts"] * order["cost_each"]
        if order.get("price_each") is not None:
            self.priced += r["parts"]
            self.price += r["parts"] * order["price_each"]

    def figures(self, cur: str) -> dict[str, Any]:
        """The money is of the parts that belong to orders (their price and cost per part); it's left blank
        when none of them has one, and the profit when any of them lacks either."""
        profit = self.price - self.cost if self.order_parts and self.costed == self.priced == self.order_parts else None
        return {
            "loops": self.loops, "failed_loops": self.failed, "parts": self.parts, "grams": round(self.grams, 1),
            "print_hours": round(self.hours, 2), "order_parts": self.order_parts,
            "cost": money(self.cost) if self.costed else None, "price": money(self.price) if self.priced else None,
            "profit": money(profit),
            "margin_pct": round(profit / self.price * 100, 1) if profit is not None and self.price else None,
            "currency": cur,
        }


def _orders_by_job(app) -> dict[tuple[str, str], dict[str, Any] | None]:
    """(printer, file) -> the order whose queued job made it; None where two orders claim the same file."""
    runs = {r["id"]: r for r in app.queue.runs()}
    found: dict[tuple[str, str], dict[str, Any] | None] = {}
    for o in app.orders.all():
        for j in (runs.get(o["run"]) or {}).get("jobs", []):
            if j.get("file"):
                key = (j["printer"], job_key(j["file"]))
                found[key] = None if key in found and found[key] is not o else o
    return found


def costs_rows(app, since: float = 0.0, until: float = math.inf, by: str = "order") -> list[dict[str, Any]]:
    """Production per order, or per day or month: loops, parts, grams, print hours, and what they cost and sell
    for. Parts and grams are of loops the monitor saw finish; hours are the prints that ended in the range."""
    if by not in BY:
        raise ValueError("by is order, day or month")
    cur = app.settings.costs.currency
    owner = _orders_by_job(app)
    fmt = {"day": "%Y-%m-%d", "month": "%Y-%m"}.get(by, "")
    groups: dict[str, _Sum] = {}
    info: dict[str, dict[str, Any] | None] = {}  # group -> its order

    def group(order, ts: float) -> _Sum:
        key = time.strftime(fmt, time.localtime(ts)) if fmt else order["id"] if order else ""
        info.setdefault(key, order)
        return groups.setdefault(key, _Sum())

    def order_of(r: dict[str, Any], name: str | None) -> dict[str, Any] | None:
        return owner.get((r["printer"], job_key(name or "")))

    for r in app.store.production(since):
        if r["ts"] < until:
            order = order_of(r, r["job"])
            group(order, r["ts"]).add_loop(r, order)
    for o in app.store.outcomes(since=since, limit=MAX_ROWS):
        if o["ts"] < until and o.get("started") and (o.get("ended") or 0) > o["started"]:
            hours = (o["ended"] - o["started"]) / 3600
            group(order_of(o, o.get("job")), o["ts"]).hours += hours
    rows = []
    for key, s in groups.items():
        order = info[key]
        if by == "order":
            head = ({"order": order["id"], "customer": order["customer"], "file": order["plate"]} if order
                    else {"order": "", "customer": "", "file": "other prints (not in an order)"})
        else:
            head = {"period": key}
        rows.append({**head, **s.figures(cur)})
    if by == "order":  # orders by when they were taken, other prints last
        created = {o["id"]: o["created"] for o in app.orders.all()}
        rows.sort(key=lambda r: (not r["order"], created.get(r["order"], 0)))
    else:
        rows.sort(key=lambda r: r["period"])
    return rows


def cost_columns(by: str) -> list[str]:
    return COST_COLUMNS["order" if by == "order" else "period"]


# ---- an invoice ----------------------------------------------------------------------------
def fmt_money(v: float | None, cur: str) -> str:
    if v is None:
        return ""
    return f"{'−' if v < 0 else ''}{cur}{' ' if cur.isalpha() else ''}{abs(v):,.2f}"


def invoice_data(app, order: dict[str, Any]) -> dict[str, Any]:
    """What an invoice shows. Anything not set stays empty, for the page to show as a blank."""
    b, costs = app.settings.business, app.settings.costs
    inv = order.get("invoice") or {}
    cur, unit, value = costs.currency, order.get("price_each"), order_value(order)
    pct = costs.tax_included_pct
    name = order["plate"]
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return {
        "business": {"name": b.name, "address": [x.strip() for x in b.address.splitlines() if x.strip()],
                     "email": b.email, "tax_id": b.tax_id, "footer": b.footer},
        "number": inv.get("label", ""), "date": inv.get("date", ""), "ordered": day(order["created"]),
        "customer": order["customer"], "notes": order.get("notes") or "",
        "lines": [{"description": name, "qty": order["quantity"], "unit": fmt_money(unit, cur),
                   "amount": fmt_money(value, cur)}],
        "total": fmt_money(value, cur),
        "tax": {"pct": pct, "amount": fmt_money(value - value / (1 + pct / 100), cur)}
        if pct and value is not None else None,
        "paid_on": day(order.get("paid_at")) if order.get("paid") else "",
    }
