"""When a run will empty an AMS tray: on which loop, and what to do about it.

Before a file is sent, for each job waiting in the queue, and while a run prints, ProofLoop adds up
what every loop draws from each tray (the slicer's grams per filament slot, through the tray the
send feeds that slot from) and compares it with what is on the tray:

* the spool linked to the tray, by ProofLoop's count: what you entered or weighed, less every loop
  charged to it since;
* otherwise the AMS's own estimate, for a Bambu spool whose RFID tag gives its weight when new:
  ``remain_pct`` of ``weight_g``. The AMS works the percentage out itself, so this is rough;
* otherwise nothing: ProofLoop says it isn't known rather than guess.

A tray that runs out mid-run has two ways on. With AMS filament backup on (a setting on the printer
that ProofLoop doesn't read), the AMS switches to another tray with the same type and colour, so
those trays are counted and named, and the advice says "if". Otherwise you swap the spool between
loops, after the last loop it still covers, instead of finding out mid-print.
"""

from __future__ import annotations

from proofloop.monitor.filament import Spool, matching_trays
from proofloop.monitor.printer.ams import EXTERNAL, label

# What goes in the send notes. "enough" and "unknown" are shown beside the printer, not as notes.
WARN = ("switch", "swap", "short", "before")


def tray_left(tray: dict, spool: Spool | None) -> dict:
    """What is on one tray: grams (None: not known), where that number comes from, and how sure it is."""
    if spool is not None and spool.remaining_g is not None:
        return {"left_g": round(max(0.0, spool.remaining_g), 1), "source": "spool", "sure": "counted",
                "says": f"ProofLoop's count for {spool.label}"}
    pct, net = tray.get("remain_pct"), tray.get("weight_g")
    if pct is not None and net:
        return {"left_g": round(net * pct / 100, 1), "source": "ams", "sure": "rough",
                "says": f"the AMS's estimate, {pct}% of a {net:g} g spool"}
    if pct is not None:
        says = f"the AMS says {pct}%, but not how much the spool held"
    elif spool is not None:
        says = f"{spool.label} has no amount yet: weigh it under Filament"
    else:
        says = "no spool is linked to it and the AMS gives no estimate"
    return {"left_g": None, "source": None, "sure": None, "says": says}


def run_of(job: dict, start: int = 1) -> tuple[list[tuple[str, str, str]], list[dict[str, float]]]:
    """A loop file ProofLoop recorded (tracking.register_job): every filament slot its plates use, and
    the grams per slot of each loop from loop ``start`` on."""
    plates = job.get("plates") or []
    seen: dict[str, tuple[str, str, str]] = {}
    for p in plates:
        for s in p["slots"]:
            seen.setdefault(str(s[0]), (str(s[0]), str(s[1]), str(s[2])))
    slots = sorted(seen.values(), key=lambda s: (not s[0].isdigit(), int(s[0]) if s[0].isdigit() else 0, s[0]))
    order = job.get("order") or [0]
    return slots, [plates[i]["grams"] for i in order[max(0, start - 1):] if 0 <= i < len(plates)]


def by_tray(run: list[dict[str, float]], feeds: dict[str, int]) -> list[dict[int, float]]:
    """Grams per slot for each loop, as grams per tray (``feeds``: slot id -> tray). Unfed slots are left out."""
    out = []
    for loop in run:
        d: dict[int, float] = {}
        for sid, g in loop.items():
            if str(sid) in feeds and g > 0:
                d[feeds[str(sid)]] = d.get(feeds[str(sid)], 0.0) + g
        out.append(d)
    return out


def _runs_out(per: list[float], have: float) -> int | None:
    """The index of the loop during which ``have`` grams run out, or None when they last."""
    used = 0.0
    for i, g in enumerate(per):
        used += g
        if used > have + 1e-6:
            return i
    return None


def _grams(g: float, sure: str | None) -> str:
    return f"about {g:.0f} g" if sure == "rough" else f"{g:.0f} g"


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def forecast(loops: list[dict[int, float]], trays: list[dict], spools: list[Spool], printer: str,
             before: int = 0, first: int = 1, printing: bool = False) -> list[dict]:
    """For each tray this run draws from: whether it lasts, and if not, the loop it runs out on and what to do.

    ``loops``: grams per tray for each loop, in print order (``by_tray``). The first ``before`` of them are
    what the printer does before this run (the rest of what it prints now, the jobs ahead in the queue):
    they use filament but aren't this run's loops. This run's loops are numbered from ``first``, and with
    ``printing`` the first of them is the one printing now (the Monitor counts on from the loop it's on).
    """
    known = {t["tray"]: t for t in trays}
    linked = {sp.tray: sp for sp in spools if sp.printer == printer and sp.tray is not None and not sp.archived}
    last = first + len(loops) - before - 1
    free = [t["label"] for t in trays if t.get("empty") and t["tray"] not in EXTERNAL]

    def number(i: int) -> int:
        return first + i - before

    rows = []
    for tid in sorted({k for loop in loops[before:] for k in loop}):
        t = known.get(tid) or {"tray": tid, "label": label(tid)}
        per = [loop.get(tid, 0.0) for loop in loops]
        need = sum(per[before:])
        main = tray_left(t, linked.get(tid))
        spare = [{"tray": b["tray"], "label": b["label"], **tray_left(b, linked.get(b["tray"]))}
                 for b in (matching_trays(trays, t["type"], t.get("color", "")) if t.get("type") else [])
                 if b["tray"] != tid]
        name = f"{t['label']} ({t.get('type') or 'filament'})"
        row = {"tray": tid, "label": t["label"], "type": t.get("type", ""), "color": t.get("color", ""),
               "need_g": round(need, 1), **main, "backups": spare, "runs_out_in": None, "swap_after": None,
               "stops": False}
        rows.append(row)
        have = main["left_g"]
        ahead = sum(per[:before])
        needs = f"the run needs {need:.0f} g" + (f" after {ahead:.0f} g for the jobs ahead of it" if ahead else "")
        if have is None:
            row["status"] = "unknown"
            row["text"] = f"{name}: how much is left isn't known ({main['says']}); {needs}"
            continue
        empties = _runs_out(per, have)
        if empties is None:
            row["status"] = "enough"
            row["text"] = f"{name}: enough. It has {_grams(have, main['sure'])} ({main['says']}); {needs}"
            continue
        pool_known = all(s["left_g"] is not None for s in spare)
        pool = have + sum(s["left_g"] or 0.0 for s in spare)
        sure = "counted" if all(x["sure"] == "counted" for x in (main, *spare)) else "rough"
        spares = _and([s["label"] for s in spare]) if spare else ""
        if spare and pool_known and _runs_out(per, pool) is None:
            row.update(status="switch", runs_out_in=number(empties) if empties >= before else None)
            when = "before this run starts" if empties < before else f"during loop {number(empties)} of {last}"
            row["text"] = (f"{name} runs out {when}; the AMS can switch to {spares} (same type and colour, "
                           f"{_grams(pool - have, sure)}), which covers the rest, if AMS filament backup is on "
                           "in the printer's settings")
            continue
        together = bool(spare and pool_known)
        k = _runs_out(per, pool) if together else empties
        assert k is not None  # with a known pool that lasts, it was a switch
        row["runs_out_in"] = number(k) if k >= before else None
        if together:  # the tray and the ones the AMS can switch to, run out together
            who, has = f"{t['label']} and {spares} ({t.get('type') or 'filament'})", f"They have {_grams(pool, sure)}"
            runs, used, hasnt, the = "run out", "are used up", "haven't", "a"
        else:
            who, has = name, f"It has {_grams(have, main['sure'])} ({main['says']})"
            runs, used, hasnt, the = "runs out", "is used up", "hasn't", "the"
        extra = []  # what else could feed it, and the numbers
        if spare and not pool_known:
            extra.append(f"{spares} has the same filament, but how much is on it isn't known")
        or_load = f", or load a second spool of it in {free[0]} for the AMS to switch to" if not spare and free else ""
        if k < before:
            row["status"] = "before"
            head = f"{who} {used} by the jobs ahead of this one on {printer}: load a new spool before it starts"
        elif k == before and printing:
            row["status"] = "short"
            head = f"{who} {runs} during this loop (loop {first} of {last}): have a new spool ready"
        elif k == before:
            row["status"] = "short"
            row["stops"] = not before and pool_known and sure == "counted"
            after = " after the jobs ahead of it" if ahead else ""
            head = f"{who} {hasnt} enough for loop {first}{after}: load a new spool before it starts{or_load}"
            extra.append(f"{has}; a loop needs {round(per[k], 1):g} g")
        else:
            row.update(status="swap", swap_after=number(k) - 1)
            head = (f"{who} {runs} during loop {number(k)} of {last}: swap {the} spool after loop "
                    f"{number(k) - 1}{or_load}")
            extra.append(f"{has}; {needs}")
        row["text"] = ". ".join([head, *extra])
    return rows
