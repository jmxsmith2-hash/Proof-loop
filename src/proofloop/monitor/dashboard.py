"""The run dashboard: parts per hour on each printer, where each open order stands, what each printer does next.

Parts per hour are counted, never estimated: the parts of the loops that ended on the printer in the last
24 hours and the last 7 days, over the hours of that period. A printer ProofLoop started counting during
the period is counted from the end of the first loop it saw (that loop's start isn't known), so a new
printer doesn't look slow. With no loop ended in the period, or less than an hour counted, there is no
number. A printer's pace while looping is the parts of its latest loops over its measured loop time (from
one loop's start to the next one's).

An open order is ``orders.progress``: parts made and still to make, and when it will be done at each
printer's measured loop time (the plan's loop time until a printer has timed a loop of its own).

Each printer's queue: the queued job it's on now, the next one waiting for it, and whether it's hands-off.
"""

from __future__ import annotations

import time
from typing import Any

from proofloop.monitor.orders import due_ts, progress
from proofloop.monitor.queue import FOLLOWED

HOUR = 3600.0
PERIODS = {"24h": 24, "7d": 24 * 7}  # hours


def rate(rows: list[dict], first: float | None, now: float, hours: float) -> dict[str, Any]:
    """Parts per hour on one printer over the last ``hours``, from its loops (``store.production`` rows)."""
    start = now - hours * HOUR
    if first is not None and first > start:
        start = first  # counting began inside the period: from the end of the first loop seen, not before
    mine = [r for r in rows if start < r["ts"] <= now]
    good = [r for r in mine if r["ok"]]
    parts = sum(r["parts"] for r in good)
    uncounted = sum(1 for r in good if not r["parts"])  # loops of files not made by this ProofLoop
    counted_h = (now - start) / HOUR
    enough = counted_h >= 1 and bool(mine) and not (uncounted and not parts)
    return {"per_hour": round(parts / counted_h, 2) if enough else None, "parts": parts, "loops": len(good),
            "failed": len(mine) - len(good), "uncounted": uncounted, "hours": round(counted_h, 1)}


def pace(rows: list[dict], loop: dict | None) -> dict[str, Any] | None:
    """Parts per hour while looping: the parts of the latest loops at the measured loop time (``timings``)."""
    if not loop:
        return None
    recent = [r["parts"] for r in rows if r["ok"] and r["parts"]][-loop["n"]:]
    if not recent:
        return None
    per_loop = sum(recent) / len(recent)
    return {"per_hour": round(per_loop * HOUR / loop["avg"], 2), "loops": loop["n"], "loop_s": round(loop["avg"])}


def dashboard(app, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    rows = app.store.production(now - max(PERIODS.values()) * HOUR)
    first = app.store.first_loops()
    orders = [o for o in app.orders.all() if o["status"] == "open"]
    for_order = {o["run"]: o["customer"] for o in orders}
    runs = app.queue.runs()

    printers = []
    for p in app.settings.printers:
        mine = [r for r in rows if r["printer"] == p.name]
        jobs = [(run, j) for run in runs for j in run["jobs"] if j["printer"] == p.name]
        on = next(((run, j) for run, j in jobs if j["status"] in FOLLOWED), None)
        waiting = [(run, j) for run, j in jobs if j["status"] == "waiting"]
        printers.append({
            "printer": p.name,
            "hands_off": p.hands_off,
            "hands_off_on": app.hands_off.on(p.name),  # hands-off and Sending to printers on
            "rate": {k: rate(mine, first.get(p.name), now, h) for k, h in PERIODS.items()},
            "pace": pace(mine, app.store.timings(p.name).get("loop")),
            "now": {"plate": on[0]["plate"], "order": for_order.get(on[0]["id"]), "status": on[1]["status"],
                    "loop": on[1].get("loop"), "loops": on[1]["loops"]} if on else None,
            "next": {"plate": waiting[0][0]["plate"], "order": for_order.get(waiting[0][0]["id"]),
                     "loops": waiting[0][1]["loops"], "held": bool(waiting[0][1].get("held"))} if waiting else None,
            "waiting_jobs": len(waiting),
            "waiting_loops": sum(j["loops"] for _, j in waiting),
            "stopped": sum(1 for _, j in jobs if j["status"] == "stopped"),  # hands-off waits for you on these
        })
    known = {k: [x["rate"][k]["per_hour"] for x in printers if x["rate"][k]["per_hour"] is not None]
             for k in PERIODS}

    out = []
    for o in orders:
        g = progress(app, o, now)
        left_on = sorted({j["printer"] for j in g["jobs"] if j["status"] != "done"})
        timed = all(app.store.timings(x).get("loop") for x in left_on)
        out.append({
            **{k: g[k] for k in ("id", "customer", "plate", "quantity", "made", "left", "pct", "due", "eta",
                                 "on_time", "late_by_days", "complete", "queued")},
            "printers": left_on,
            "eta_from": None if g["eta"] is None else "measured" if timed else "planned",
        })
    out.sort(key=lambda o: (due_ts(o["due"]) if o["due"] else float("inf"), o["eta"] or float("inf")))
    return {
        "at": now,
        "printers": printers,
        "farm": {k: round(sum(v), 2) if v else None for k, v in known.items()},  # printers with a number
        "orders": out,
    }
