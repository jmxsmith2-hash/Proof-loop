"""What happened on your printers while you were away, in one summary.

Parts and loops made, failures and the likely cause (the layer, the printer's HMS alerts), plates
with parts left behind, what's waiting for you now, filament used and spools running low, and where
the queue and your orders stand. ``summary(app)`` returns it as data with a ready-to-read ``text``; the window
shows it on the Overview, ``proofloop summary`` prints it, assistants read it through
``proofloop mcp``, and with *Morning summary* set it arrives through your notification targets
once a day.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HOUR = 3600
NEEDS_YOU = {"plate_not_clear", "check_failed", "filament_short", "queue_stopped", "safety_pause", "paused", "hms",
             "offline", "printer_error"}


def _plural(n: float, word: str) -> str:
    n = round(n) if isinstance(n, float) else n
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary(app, hours: float = 12, since: float | None = None) -> dict[str, Any]:
    now = time.time()
    since = since if since is not None else now - hours * HOUR
    names = [p.name for p in app.settings.printers]
    prod = [r for r in app.store.production(since) if r["ts"] >= since]
    outcomes = app.store.outcomes(since=since, limit=2000)
    events = [e for e in app.store.events(1000) if e["ts"] >= since]

    printers = []
    for name in names:
        w = app.watches.get(name)
        st = w.link.state if w else None
        mine = [r for r in prod if r["printer"] == name]
        ended = [o for o in outcomes if o["printer"] == name]
        printers.append({
            "printer": name,
            "online": bool(st and st.online),
            "state": (st.gcode_state or "unknown").lower() if st and st.online else "offline",
            "status": w.status if w else "",
            "waiting_for_you": bool(w and w.waiting_for_you),
            "parts": sum(r["parts"] for r in mine if r["ok"]),
            "loops": sum(1 for r in mine if r["ok"]),
            "failed_loops": sum(1 for r in mine if not r["ok"]),
            "prints": {k: sum(1 for o in ended if o["outcome"] == k) for k in ("finished", "failed", "cancelled")},
        })

    failures = [{
        "printer": o["printer"], "job": o.get("job"), "outcome": o["outcome"], "when": o.get("ended") or o["ts"],
        "loop": o.get("loop_no") or None, "layer": o.get("layer"), "total_layers": o.get("total_layers"),
        "hms": [h["code"] for h in o.get("hms") or []], "error": o.get("print_error"),
        "picture": o.get("picture"),
    } for o in outcomes if o["outcome"] in ("failed", "cancelled")]
    parts_left = [{"printer": o["printer"], "job": o.get("job"), "loop": o.get("loop_no"),
                   "picture": o.get("eject_picture")} for o in outcomes if o.get("eject") == "parts_left"]

    attention, seen = [], set()
    for p in printers:
        if p["waiting_for_you"]:
            attention.append({"printer": p["printer"], "what": "waiting", "message": p["status"] or "waiting for you"})
            seen.add((p["printer"], "waiting"))
    waiting = {p["printer"] for p in printers if p["waiting_for_you"]}
    for e in events:  # newest first: one line per printer and kind; a waiting printer already has its line
        key = (e["printer"], e["kind"])
        if e["printer"] in waiting:
            continue
        if e["level"] in ("error", "warning") and e["kind"] in NEEDS_YOU and key not in seen:
            seen.add(key)
            attention.append({"printer": e["printer"], "what": e["kind"], "when": e["ts"], "message": e["message"]})

    used = [u for u in app.store.usage(since=since, limit=5000) if u.get("kind") in ("loop", "partial", "manual")]
    by_material: dict[str, float] = {}
    spools = {s.id: s for s in app.inventory.all()}
    for u in used:
        sp = spools.get(u.get("spool_id") or "")
        mat = sp.material if sp else (u.get("material") or "unassigned")
        by_material[mat] = round(by_material.get(mat, 0) + (u.get("grams") or 0), 1)
    low_g = app.settings.filament.low_g
    low = [{"spool": s.label, "material": s.material, "color": s.color, "left_g": s.remaining_g,
            "status": s.status(low_g), "days_left": app.inventory.forecast(s).get("days_left")}
           for s in app.inventory.all(archived=False) if s.status(low_g) in ("low", "empty")]
    unmatched = round(sum(u.get("grams") or 0 for u in app.store.usage(unmatched=True, limit=5000)), 1)

    jobs = [(run, j) for run in app.queue.runs() for j in run["jobs"]]
    queue: dict[str, Any] = {k: sum(1 for _, j in jobs if j["status"] == k)
                             for k in ("waiting", "sent", "printing", "done", "stopped")}
    queue["next"] = [{"printer": j["printer"], "plate": run["plate"], "loops": j["loops"]}
                     for run, j in jobs if j["status"] == "waiting"][:5]

    from proofloop.monitor.orders import progress

    orders = [progress(app, o, now) for o in app.orders.all() if o["status"] == "open"]
    for o in orders:
        if o["on_time"] is False:
            attention.append({"printer": None, "what": "order_late", "when": None,
                              "message": f"the order for {o['customer']} will be {o['late_by_days']} day"
                                         f"{'s' if o['late_by_days'] != 1 else ''} late ({o['made']} of "
                                         f"{o['quantity']} made)"})

    out = {
        "since": since, "until": now, "hours": round((now - since) / HOUR, 1),
        "parts": sum(p["parts"] for p in printers), "loops": sum(p["loops"] for p in printers),
        "failed_loops": sum(p["failed_loops"] for p in printers),
        "printers": printers, "attention": attention, "failures": failures, "parts_left": parts_left,
        "filament": {"used_g": round(sum(by_material.values()), 1), "by_material": by_material, "low": low,
                     "unassigned_g": unmatched},
        "queue": queue,
        "orders": [{k: o[k] for k in ("id", "customer", "plate", "quantity", "made", "due", "eta", "on_time",
                                      "late_by_days")} for o in orders],
    }
    out["headline"] = _headline(out)
    out["text"] = _text(out)
    return out


def _headline(s: dict) -> str:
    if not s["printers"]:
        return "No printers yet: add one under Printers › Monitor."
    made = (f"{_plural(s['parts'], 'part')} in {_plural(s['loops'], 'loop')}" if s["loops"]
            else "nothing finished")
    need = len(s["attention"])
    tail = f"; {_plural(need, 'thing')} need{'s' if need == 1 else ''} you" if need else "; nothing needs you"
    return f"{made} in the last {_plural(s['hours'], 'hour')}{tail}."


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""


def _text(s: dict) -> str:
    lines = [s["headline"]]
    for p in s["printers"]:
        bits = [f"{_plural(p['parts'], 'part')}, {_plural(p['loops'], 'loop')}"]
        if p["failed_loops"]:
            bits.append(f"{p['failed_loops']} failed")
        bits.append("waiting for you" if p["waiting_for_you"] else p["state"])
        lines.append(f"• {p['printer']}: {', '.join(bits)}")
    if s["attention"]:
        lines.append("Needs you:")
        lines += [f"  – {a['printer'] + ': ' if a['printer'] else ''}{a['message']}" for a in s["attention"]]
    for f in s["failures"][:5]:
        where = f" at layer {f['layer']}/{f['total_layers']}" if f.get("layer") and f.get("total_layers") else ""
        hms = f", HMS {', '.join(f['hms'])}" if f["hms"] else ""
        lines.append(f"  ✖ {f['printer']} {f['outcome']} {f['job'] or ''}{where}{hms} ({_when(f['when'])})")
    fil = s["filament"]
    if fil["used_g"]:
        mats = ", ".join(f"{g:g} g {m}" for m, g in sorted(fil["by_material"].items(), key=lambda kv: -kv[1]))
        lines.append(f"Filament: {mats}.")
    if fil["low"]:
        lines.append("Running low: " + ", ".join(f"{x['spool']} ({x['left_g']:.0f} g)" if x["left_g"] is not None
                                                  else x["spool"] for x in fil["low"]) + ".")
    if fil["unassigned_g"]:
        lines.append(f"{fil['unassigned_g']:g} g of filament isn't matched to a spool yet (Filament).")
    if s["orders"]:
        lines.append("Orders: " + "; ".join(
            f"{o['customer']} {o['made']}/{o['quantity']}" + (f", due {o['due']}" if o["due"] else "")
            + (" (late)" if o["on_time"] is False else "") for o in s["orders"]) + ".")
    q = s["queue"]
    if q["waiting"] or q["printing"]:
        lines.append(f"Queue: {q['printing']} printing, {q['waiting']} waiting.")
    return "\n".join(lines)


class Morning:
    """Sends the summary once a day at the time you chose, covering the time since the last one."""

    def __init__(self, folder: Path):
        self.path = Path(folder) / "summary-sent.json"

    def _last(self) -> float | None:
        try:
            return float(json.loads(self.path.read_text(encoding="utf-8"))["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def due(self, at: str | None, now: float | None = None) -> float | None:
        """When the summary is due now: the time it should cover from. None when it isn't."""
        if not at:
            return None
        now = time.time() if now is None else now
        hh, mm = (int(x) for x in at.split(":"))
        today = datetime.fromtimestamp(now).replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
        last = self._last()
        if now < today or (last is not None and last >= today):
            return None
        return max(last or 0, now - 24 * HOUR)

    def sent(self, now: float | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"at": time.time() if now is None else now}), encoding="utf-8")
