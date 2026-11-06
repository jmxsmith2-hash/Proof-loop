"""Grams and cost of every print the monitor saw end, whether or not it was a ProofLoop loop file.

The grams come from what is known about the job:

* a file ProofLoop made: the slicer's grams per filament slot (a print that stopped part-way gets the G-code's
  own extrusion up to the last layer it finished, or, without that, the share of layers it got through);
* anything else: unknown. The printer's report carries no filament weight, so a file ProofLoop didn't make is
  listed as unknown and left out of the totals instead of guessed. (The filament type it fed from is kept.)

The cost uses your saved cost settings through ``costing.cost``: filament at the price of the spool that fed
it, then electricity, printer wear and the rest you set. Failed-loop surcharges and one-off setup labour are
left out (this print is the one that failed or the only one). It is recorded when the print ends
(``PrintJournal`` calls ``price_outcome``); older records are priced when listed.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from proofloop.loopfiles.gcode.usage import used_by_layer
from proofloop.monitor.costing import Times, cost
from proofloop.monitor.filament import prices_for
from proofloop.monitor.tracking import FilamentTracker, job_key


def _types(o: dict) -> list[str]:
    seen: list[str] = []
    for f in o.get("filaments") or []:
        t = str(f.get("type") or "").upper()
        if t and t not in seen:
            seen.append(t)
    return seen


def _grams_of(o: dict, plate: dict) -> tuple[dict[str, float] | None, str]:
    """Grams per slot of this print, and where they came from."""
    grams = {k: float(v) for k, v in (plate.get("grams") or {}).items()}
    layer, total = int(o.get("layer") or 0), int(o.get("total_layers") or 0)
    if o.get("outcome") == "finished" or (total and layer >= total):
        return grams, "slicer"
    used = used_by_layer(plate.get("layers"), layer)
    if used is not None:
        return {k: float(v) for k, v in used.items()}, f"G-code, up to layer {layer}"
    if total:
        share = min(1.0, layer / total)
        return {k: g * share for k, g in grams.items()}, f"estimated from layer {layer} of {total}"
    return None, ""


def price_outcome(app: Any, o: dict, trays: list[dict] | None = None) -> dict:
    """What one print used and cost. ``grams`` is None (and ``cost`` too) when the job's filament isn't known."""
    seconds = 0.0
    if o.get("started"):
        seconds = max(0.0, float((o.get("ended") or o.get("ts") or 0) - o["started"]))
    out: dict[str, Any] = {"grams": None, "grams_source": "", "filaments": _types(o), "by_type": {},
                           "material_cost": None, "cost": None, "cost_complete": False, "seconds": round(seconds),
                           "note": "the filament used isn't known: this file wasn't made by ProofLoop"}
    job = job_key(o["job"]) if o.get("job") else ""
    data = app.store.job(job) if job else None
    if not data or not data.get("plates"):
        return out
    plate = FilamentTracker.plate_for(data, int(o.get("loop_no") or 1))
    slot_grams, source = _grams_of(o, plate)
    if slot_grams is None:
        out["note"] = "the print stopped before its layers were known"
        return out
    slots = [tuple(s) for s in plate["slots"]]
    out["filaments"] = sorted({m.upper() for _, m, _ in slots if m}) or out["filaments"]
    out.update(grams=round(sum(slot_grams.values()), 2), grams_source=source, note="")
    c = app.settings.costs.model_copy(update={"failure_rate_pct": 0, "labor_min_per_run": None, "extras_per_part": []})
    prices = prices_for(app.inventory, o.get("printer"), slots, trays or [])
    rep = cost(c, slots, slot_grams, 1, Times(seconds, 0.0, None), 1, prices)
    by_type: dict[str, dict] = {}
    for m in rep.materials:
        t = by_type.setdefault(str(m["type"]).upper(), {"grams": 0.0, "material_cost": 0.0, "priced": True})
        t["grams"] += m["grams"]
        if m["cost"] is None:
            t["priced"] = False
        else:
            t["material_cost"] += m["cost"]
    out["by_type"] = {k: {"grams": round(v["grams"], 2),
                          "material_cost": round(v["material_cost"], 4) if v["priced"] else None}
                      for k, v in by_type.items()}
    out["material_cost"] = next((ln.per_loop for ln in rep.lines if ln.key == "material"), None)
    if any(ln.per_loop is not None for ln in rep.lines):
        out["cost"] = rep.totals["per_loop"]
    out["cost_complete"] = not [m for m in rep.missing if not m.startswith("cooldown")]
    return out


def _empty() -> dict:
    return {"prints": 0, "grams": 0.0, "material_cost": 0.0, "cost": 0.0, "unknown_grams": 0, "unpriced": 0,
            "by_type": {}}


def _add(tot: dict, p: dict) -> None:
    tot["prints"] += 1
    if p.get("grams") is None:
        tot["unknown_grams"] += 1
        return
    tot["grams"] += p["grams"]
    for t, v in (p.get("by_type") or {}).items():
        row = tot["by_type"].setdefault(t, {"type": t, "prints": 0, "grams": 0.0, "material_cost": 0.0})
        row["prints"] += 1
        row["grams"] += v["grams"]
        row["material_cost"] += v["material_cost"] or 0.0
    if p.get("material_cost") is None:
        tot["unpriced"] += 1
    else:
        tot["material_cost"] += p["material_cost"]
    if p.get("cost") is not None:
        tot["cost"] += p["cost"]


def _finish(tot: dict, label: str) -> dict:
    tot["by_type"] = sorted(({**r, "grams": round(r["grams"], 1), "material_cost": round(r["material_cost"], 2)}
                             for r in tot["by_type"].values()), key=lambda r: -r["grams"])
    tot.update(label=label, grams=round(tot["grams"], 1), material_cost=round(tot["material_cost"], 2),
               cost=round(tot["cost"], 2))
    notes = []
    n = tot["unknown_grams"]
    if n:
        notes.append(f"{n} print{'s' if n > 1 else ''} with unknown filament {'are' if n > 1 else 'is'} "
                     "left out of these totals")
    n = tot["unpriced"]
    if n:
        notes.append(f"{n} print{'s' if n > 1 else ''} used filament with no price set, so the cost is a minimum")
    tot["note"] = "; ".join(notes)
    return tot


def history(app: Any, limit: int = 100, since: float = 0.0, printer: str | None = None) -> dict:
    """Recent prints newest first with grams and cost, plus this month's totals and the totals of the list.

    Each row keeps what a reprint needs: ``id`` (``/api/outcomes/{id}/file`` serves the kept copy when
    ``has_file``), ``job`` as the printer reported it, ``file`` (the normalised file name) and ``printer``.
    """
    limit = min(max(1, limit), 2000)

    def row(o: dict) -> dict:
        p = o.get("costing") or price_outcome(app, o)
        return {
            "id": o["id"], "ts": o.get("ended") or o["ts"], "started": o.get("started"),
            "seconds": p.get("seconds") or None,
            "printer": o["printer"], "job": o.get("job") or "", "file": job_key(o["job"]) if o.get("job") else "",
            "loop_no": o.get("loop_no"), "loop_file": bool(o.get("loop_file")),
            "has_file": bool(o.get("file")), "model_id": o.get("model_id", ""),
            "outcome": o["outcome"], "layer": o.get("layer"), "total_layers": o.get("total_layers"),
            "grams": p.get("grams"), "grams_known": p.get("grams") is not None,
            "grams_source": p.get("grams_source", ""),
            "filaments": p.get("filaments", []), "material_cost": p.get("material_cost"), "cost": p.get("cost"),
            "cost_complete": p.get("cost_complete", False), "note": p.get("note", ""),
            "recorded": bool(o.get("costing")), "by_type": p.get("by_type", {}),
        }

    rows = [row(o) for o in app.store.outcomes(since=since, limit=limit, printer=printer)]
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = _empty()
    for o in app.store.outcomes(since=month_start.timestamp(), limit=20000, printer=printer):
        _add(month, row(o))
    listed = _empty()
    for r in rows:
        _add(listed, r)
    return {"currency": app.settings.costs.currency, "generated": time.time(), "rows": rows,
            "month": _finish(month, now.strftime("%B %Y")), "listed": _finish(listed, f"these {len(rows)} prints")}
