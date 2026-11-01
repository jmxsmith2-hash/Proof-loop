"""Maintenance per printer: how much each one has been used since each task was last done.

ProofLoop already records every print (how long it ran, how it ended), every loop and the filament
used. For each printer and each maintenance task it counts those since the task was last marked done
(or, until then, since ProofLoop first saw the task), and shows how close the task is to its interval.
Intervals are in print hours, prints, loops, grams of filament or days. The built-in tasks and their
intervals are starting points you edit in *Printers › Maintenance*, not manufacturer figures, except
where ``source`` says where Bambu Lab gives one (``docs/sources.md``).

When a task becomes due you get one notice through your notification targets, and not again until it
is done or a week has passed. The daily summary lists what is due. Maintenance never blocks or pauses
a print.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

HOUR, DAY = 3600.0, 86400.0
RENOTICE_S = 7 * DAY
MAX_PRINT_S = 72 * HOUR  # a recorded print longer than this was a missed end, not print time
Unit = Literal["hours", "prints", "loops", "grams", "days"]
UNITS: tuple[str, ...] = ("hours", "prints", "loops", "grams", "days")
SOON, OVERDUE = 0.8, 1.25  # share of the interval: "soon" from 80 %, "overdue" from 125 %


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TaskEdit(_Model):
    """What you changed on a built-in task."""

    every: float | None = Field(None, gt=0, le=100000)
    unit: Unit | None = None
    enabled: bool | None = None


class CustomTask(_Model):
    id: str
    name: str = Field(min_length=1, max_length=60)
    every: float = Field(gt=0, le=100000)
    unit: Unit = "hours"
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("give the task a name")
        return v


class MaintenanceSettings(_Model):
    remind: bool = True  # one notice when a task becomes due
    edits: dict[str, TaskEdit] = Field(default_factory=dict)  # built-in tasks you changed, by id
    custom: list[CustomTask] = Field(default_factory=list)


# Starting points. "source" is set only where Bambu Lab gives the interval (docs/sources.md, ref BW).
BUILTIN: tuple[dict[str, Any], ...] = (
    {"id": "plate", "name": "Clean the build plate", "every": 25, "unit": "prints",
     "hint": "Wash it with warm water and dish soap, or wipe it with isopropyl alcohol."},
    {"id": "purge", "name": "Empty the purge chute", "every": 50, "unit": "prints",
     "hint": "Waste from colour changes collects here. Empty it before it fills up."},
    {"id": "nozzle", "name": "Clean and check the nozzle", "every": 100, "unit": "hours",
     "hint": "Look for a worn or partly blocked tip. Do a cold pull if prints are under-extruding."},
    {"id": "nozzle_change", "name": "Change the nozzle", "every": 500, "unit": "hours",
     "hint": "Sooner with abrasive filament such as carbon-fibre or glow in the dark."},
    {"id": "belts", "name": "Check belt tension", "every": 300, "unit": "hours",
     "hint": "Loose belts show up as layer shifts and ringing."},
    {"id": "rails", "name": "Clean and oil the rails", "every": 200, "unit": "hours",
     "hint": "Steel rods only. Check your printer's own guide: not every model oils its rods."},
    {"id": "carbon", "name": "Clean the carbon rods", "every": 30, "unit": "days", "source": "BW",
     "hint": "Isopropyl alcohol and a lint-free cloth. No grease on carbon rods."},
    {"id": "screws", "name": "Grease the lead screws", "every": 90, "unit": "days", "source": "BW",
     "hint": "Clean off dust first, then a thin coat of grease."},
    {"id": "desiccant", "name": "Check the AMS desiccant", "every": 30, "unit": "days",
     "hint": "Change it or dry it when the indicator is no longer blue."},
)
BUILTIN_IDS = {t["id"] for t in BUILTIN}


def tasks(cfg: MaintenanceSettings) -> list[dict[str, Any]]:
    """Every task as it is now: the built-in ones with your changes, then your own."""
    out = []
    for t in BUILTIN:
        e = cfg.edits.get(t["id"]) or TaskEdit()
        out.append({
            "id": t["id"], "name": t["name"], "hint": t.get("hint", ""), "builtin": True,
            "source": t.get("source"), "default": {"every": t["every"], "unit": t["unit"]},
            "every": e.every if e.every is not None else t["every"], "unit": e.unit or t["unit"],
            "enabled": e.enabled if e.enabled is not None else True,
        })
    for c in cfg.custom:
        out.append({"id": c.id, "name": c.name, "hint": "", "builtin": False, "source": None, "default": None,
                    "every": c.every, "unit": c.unit, "enabled": c.enabled})
    return out


def state_of(used: float, every: float) -> str:
    share = used / every if every else 0.0
    return "overdue" if share >= OVERDUE else "due" if share >= 1 else "soon" if share >= SOON else "ok"


# ---- what a printer has done ------------------------------------------------------------
def _events(store, printer: str, since: float) -> list[tuple[float, str, float]]:
    """(when, counter, amount) for every print, loop and gram recorded on ``printer`` since ``since``."""
    ev: list[tuple[float, str, float]] = []
    for o in store.outcomes(since=since, limit=100000, printer=printer):
        s, e = o.get("started"), o.get("ended")
        secs = e - s if isinstance(s, (int, float)) and isinstance(e, (int, float)) else 0.0
        ev.append((o["ts"], "prints", 1.0))
        ev.append((o["ts"], "hours", secs / HOUR if 0 < secs <= MAX_PRINT_S else 0.0))
    ev += [(r["ts"], "loops", 1.0) for r in store.production(since) if r["printer"] == printer and r["ok"]]
    ev += [(u["ts"], "grams", float(u.get("grams") or 0)) for u in store.usage(since=since, limit=100000)
           if u.get("printer") == printer and u.get("kind") in ("loop", "partial")]
    return ev


def _used(events, since: float, until: float) -> dict[str, float]:
    c = {"hours": 0.0, "prints": 0.0, "loops": 0.0, "grams": 0.0}
    for ts, kind, amount in events:
        if since < ts <= until:  # something at the very moment of a Done was before it
            c[kind] += amount
    c["days"] = max(0.0, (until - since) / DAY)
    return c


def _round(unit: str, v: float) -> float:
    return round(v) if unit in ("prints", "loops", "grams") else round(v, 1)


def _item(t: dict, row: dict, counted: dict[str, float]) -> dict[str, Any]:
    used = counted[t["unit"]]
    return {
        "task": t["id"], "name": t["name"], "hint": t["hint"], "builtin": t["builtin"], "source": t["source"],
        "unit": t["unit"], "every": t["every"], "used": _round(t["unit"], used),
        "left": _round(t["unit"], max(0.0, t["every"] - used)),
        "pct": round(100 * used / t["every"]), "state": state_of(used, t["every"]),
        "last_done": row["done"], "note": row["note"] or "", "since": row["since"],
        "counted": {k: _round(k, v) for k, v in counted.items()},
    }


def _rows(app, printer: str, active: list[dict], now: float) -> dict[str, dict]:
    """The saved state of each task on a printer; a task seen for the first time starts counting now."""
    rows = app.store.maintenance(printer)
    for t in active:
        if t["id"] not in rows:
            app.store.maintenance_start(printer, t["id"], now)
            rows[t["id"]] = {"since": now, "done": None, "note": "", "notified": None}
    return rows


def printer_status(app, printer: str, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    active = [t for t in tasks(app.settings.maintenance) if t["enabled"]]
    if not active:
        return []
    rows = _rows(app, printer, active, now)
    events = _events(app.store, printer, min(rows[t["id"]]["since"] for t in active))
    return [_item(t, rows[t["id"]], _used(events, rows[t["id"]]["since"], now)) for t in active]


def status(app, now: float | None = None) -> dict[str, Any]:
    """Every printer's tasks with how much is used, and the task list for editing."""
    now = time.time() if now is None else now
    printers = []
    for p in app.settings.printers:
        items = printer_status(app, p.name, now)
        printers.append({"printer": p.name, "tasks": items,
                         "due": sum(1 for i in items if i["state"] in ("due", "overdue"))})
    return {"printers": printers, "tasks": tasks(app.settings.maintenance), "remind": app.settings.maintenance.remind,
            "due": sum(p["due"] for p in printers), "now": now}


def due(app, now: float | None = None) -> list[dict[str, Any]]:
    """The tasks that are due now, on every printer (for the summary)."""
    out = []
    for p in status(app, now)["printers"]:
        out += [{"printer": p["printer"], **i} for i in p["tasks"] if i["state"] in ("due", "overdue")]
    return out


# ---- marking a task done ----------------------------------------------------------------
def mark_done(app, printer: str, task: str, note: str = "", on: str | None = None,
              now: float | None = None) -> dict[str, Any]:
    """Record that ``task`` was done on ``printer`` (``on``: a day, YYYY-MM-DD; default now) and count from there.

    Raises KeyError for an unknown printer or task, and ValueError for a day in the future or not a date.
    """
    now = time.time() if now is None else now
    if not any(p.name == printer for p in app.settings.printers):
        raise KeyError(f"no printer called {printer!r}")
    t = next((x for x in tasks(app.settings.maintenance) if x["id"] == task), None)
    if t is None:
        raise KeyError(f"no maintenance task {task!r}")
    when = now
    if on:
        d = date.fromisoformat(on)
        if d > datetime.fromtimestamp(now).date():
            raise ValueError("that day hasn't happened yet")
        when = now if d == datetime.fromtimestamp(now).date() else datetime(d.year, d.month, d.day, 12).timestamp()
    before = _rows(app, printer, [t], now)[task]["since"]
    used = _used(_events(app.store, printer, before), before, when)
    app.store.maintenance_done(printer, task, when, note.strip()[:200], {k: _round(k, v) for k, v in used.items()})
    if not t["enabled"]:
        return {"task": task, "name": t["name"], "state": "off", "last_done": when}
    return next(i for i in printer_status(app, printer, now) if i["task"] == task)


# ---- the notice -----------------------------------------------------------------------------
def unit_text(unit: str, n: float) -> str:
    word = {"hours": "print hour", "prints": "print", "loops": "loop", "grams": "g of filament", "days": "day"}[unit]
    if unit == "grams":
        return f"{n:g} g of filament"
    return f"{n:g} {word}{'' if n == 1 else 's'}"


def notices(app, now: float | None = None) -> list[tuple[str, str]]:
    """(printer, message) for each task that has just become due, or has been due for a week without being done.

    Each is returned once: it is marked as told, and the mark is cleared when the task is done or no longer due.
    """
    now = time.time() if now is None else now
    cfg = app.settings.maintenance
    out = []
    for p in app.settings.printers:
        rows = app.store.maintenance(p.name)
        for i in printer_status(app, p.name, now):
            told = (rows.get(i["task"]) or {}).get("notified")
            if i["state"] not in ("due", "overdue"):
                if told is not None:
                    app.store.maintenance_notified(p.name, i["task"], None)
                continue
            if not cfg.remind or (told is not None and now - told < RENOTICE_S):
                continue
            app.store.maintenance_notified(p.name, i["task"], now)
            out.append((p.name, f"Maintenance {i['state']}: {i['name']} on {p.name} "
                                f"({unit_text(i['unit'], i['used'])} since it was last done, every "
                                f"{unit_text(i['unit'], i['every'])}). Mark it done under Printers › Maintenance."))
    return out
