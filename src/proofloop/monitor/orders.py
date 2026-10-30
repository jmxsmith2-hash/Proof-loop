"""Orders: what someone asked for, by when, and whether your printers will make it in time.

An order is a quantity of a plate's parts for a customer, with a due date and, if you like, a price.
Taking an order plans its run over the printers that can print the plate (``queue.spread``) and
keeps it in the queue, so it's made the same way as any queued run: you send each job, nothing
starts by itself. From then on the order follows its jobs: parts made (finished loops of its jobs),
when it will be done (each printer's remaining loops at its own measured loop time), whether that's
before the due date, and what it costs and earns.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

DAY = 86400.0


def due_ts(due: str) -> float:
    """The end of the due day, local time."""
    d = date.fromisoformat(due)
    return datetime(d.year, d.month, d.day, 23, 59, 59).timestamp()


class Orders:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, orders: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(orders, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._load()

    def get(self, oid: str) -> dict[str, Any]:
        for o in self.all():
            if o["id"] == oid:
                return o
        raise KeyError(f"no order {oid}")

    def add(self, customer: str, plate: str, quantity: int, due: str | None, run: dict, plan: dict,
            price_each: float | None = None, cost_each: float | None = None, notes: str = "") -> dict[str, Any]:
        if due:
            due_ts(due)  # a real date, or ValueError
        rows = {r["printer"]: r for r in plan["rows"]}
        order = {
            "id": uuid.uuid4().hex[:8], "customer": customer.strip() or "—", "plate": plate,
            "quantity": int(quantity), "due": due or None, "price_each": price_each, "cost_each": cost_each,
            "notes": notes.strip(), "created": time.time(), "status": "open", "run": run["id"],
            "parts_per_loop": max(1, round(plan["parts"] / max(1, plan["loops"]))),
            # each printer's loop time when the run was planned, for the forecast until it has measured its own
            "loop_s": {p: (r["finish_s"] - r["starts_in_s"]) / r["loops"] for p, r in rows.items() if r["loops"]},
            "told": False,
        }
        with self._lock:
            orders = self._load()
            orders.append(order)
            self._save(orders)
        return order

    def update(self, oid: str, **changes) -> dict[str, Any]:
        with self._lock:
            orders = self._load()
            for o in orders:
                if o["id"] == oid:
                    o.update(changes)
                    self._save(orders)
                    return o
        raise KeyError(f"no order {oid}")

    def remove(self, oid: str) -> dict[str, Any]:
        with self._lock:
            orders = self._load()
            gone = next((o for o in orders if o["id"] == oid), None)
            if gone is None:
                raise KeyError(f"no order {oid}")
            self._save([o for o in orders if o["id"] != oid])
        return gone


def progress(app, order: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """The order as it stands: parts made, when it will be done, on time or not, money."""
    now = time.time() if now is None else now
    try:
        run = app.queue.get(order["run"])
        jobs = run["jobs"]
    except KeyError:
        run, jobs = None, []
    ppl = order["parts_per_loop"]
    loops_done, left_by_printer = 0, dict[str, int]()
    for j in jobs:
        if j["status"] == "done":
            done = j["loops"]
        elif j["status"] == "printing":
            done = max(0, (j.get("loop") or 1) - 1)
        else:
            done = 0
        loops_done += done
        if j["status"] not in ("done",):
            left_by_printer[j["printer"]] = left_by_printer.get(j["printer"], 0) + j["loops"] - done
    made = min(order["quantity"], loops_done * ppl)
    eta = None
    if made < order["quantity"] and left_by_printer:
        finish = []
        for printer, left in left_by_printer.items():
            measured = app.store.timings(printer).get("loop", {}).get("avg")
            loop_s = measured or order["loop_s"].get(printer) or max(order["loop_s"].values(), default=3600.0)
            finish.append(left * loop_s)
        eta = now + max(finish)
    complete = made >= order["quantity"] or (jobs and all(j["status"] == "done" for j in jobs))
    due = due_ts(order["due"]) if order.get("due") else None
    if complete:
        on_time = None if due is None else True
    else:
        on_time = None if due is None or eta is None else eta <= due
    money = {}
    if order.get("price_each") is not None:
        money["revenue"] = round(order["price_each"] * order["quantity"], 2)
    if order.get("cost_each") is not None:
        money["cost"] = round(order["cost_each"] * order["quantity"], 2)
    if len(money) == 2:
        money["profit"] = round(money["revenue"] - money["cost"], 2)
    return {
        **{k: v for k, v in order.items() if k not in ("loop_s", "told")},
        "made": made, "left": order["quantity"] - made, "pct": round(100 * made / order["quantity"]),
        "complete": bool(complete), "eta": eta, "on_time": on_time,
        "late_by_days": math.ceil((eta - due) / DAY) if on_time is False and eta and due else None,
        "queued": run is not None, "jobs": [{k: j.get(k) for k in ("printer", "loops", "status", "loop")}
                                            for j in jobs],
        "money": money,
    }


def finished(app) -> list[dict[str, Any]]:
    """Open orders that have just been made in full: marked done, once each."""
    out = []
    for o in app.orders.all():
        if o["status"] != "open" or o.get("told"):
            continue
        p = progress(app, o)
        if p["complete"]:
            app.orders.update(o["id"], status="done", told=True, done_at=time.time())
            out.append(p)
    return out
