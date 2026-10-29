"""Filament inventory: your spools, what each plate needs, and what each loop used.

Nothing here is guessed. A spool's price, weight, empty-spool weight and
remaining filament are yours to enter (or weigh, or import from Spoolman).
What a loop uses comes from the file: the slicer's grams per filament slot for
a finished loop, and the G-code's own extrusion up to the last finished layer
for a loop that stopped part-way (see gcode/usage.py). Which spool fed a loop
comes from the printer: the AMS tray it fed from (``tray_now``), a single AMS
tray with the same type and colour, the RFID tag on Bambu spools, or the
spool you picked. When none of these settles it, the usage is kept as
"not matched" for you to assign, never charged to a guess.
"""

from __future__ import annotations

import math
import re
import secrets
import time

from pydantic import BaseModel, ConfigDict, Field, field_validator

DAY = 86400


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


def norm_color(v: str) -> str:
    v = (v or "").strip().lstrip("#").upper()
    return f"#{v[:6]}" if re.fullmatch(r"[0-9A-F]{6}([0-9A-F]{2})?", v) else ""


def slot_key(material: str, color: str) -> str:
    return f"{(material or '').upper()}|{norm_color(color)}"


class Spool(_M):
    id: str = ""
    name: str = ""
    brand: str = ""
    material: str = Field(min_length=1)  # PLA, PETG... matched against the slicer's filament type
    color: str = ""  # #RRGGBB
    preset: str = ""  # slicer filament preset, e.g. "Bambu PLA Basic @BBL P2S"
    filament_id: str = ""  # Bambu filament id (the AMS tray_info_idx), e.g. GFA00
    price: float | None = Field(None, ge=0)  # what you paid for this spool
    net_g: float | None = Field(None, gt=0)  # filament on the spool when new, as sold
    tare_g: float | None = Field(None, ge=0)  # the empty spool: weigh one to know it
    remaining_g: float | None = None
    diameter: float | None = Field(None, gt=0)
    density: float | None = Field(None, gt=0)
    low_g: float | None = Field(None, ge=0)  # warn below this
    location: str = ""
    printer: str = ""  # loaded in this printer...
    tray: int | None = None  # ...in this AMS tray (global id), set by the AMS link
    tag: str = ""  # RFID tray_uuid / tag_uid of a Bambu spool
    lot: str = ""
    notes: str = ""
    added: float = 0.0
    opened: float | None = None
    dried: float | None = None
    drying_temp: float | None = None  # from the slicer preset or the spool's RFID tag
    drying_hours: float | None = None
    last_used: float | None = None
    archived: bool = False
    spoolman_id: int | None = None

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        return norm_color(v)

    @field_validator("material")
    @classmethod
    def _material(cls, v: str) -> str:
        return v.strip().upper()

    @property
    def per_kg(self) -> float | None:
        return self.price / (self.net_g / 1000) if self.price is not None and self.net_g else None

    @property
    def label(self) -> str:
        return self.name or " ".join(x for x in (self.brand, self.material, self.color) if x)

    def status(self, default_low: float | None = None) -> str:
        if self.archived:
            return "archived"
        if self.remaining_g is None:
            return "unknown"
        if self.remaining_g <= 0:
            return "empty"
        low = self.low_g if self.low_g is not None else default_low
        return "low" if low is not None and self.remaining_g <= low else "ok"

    def as_dict(self, default_low: float | None = None) -> dict:
        d = self.model_dump()
        d.update(
            label=self.label,
            per_kg=None if self.per_kg is None else round(self.per_kg, 4),
            remaining_pct=(
                round(100 * self.remaining_g / self.net_g, 1)
                if self.remaining_g is not None and self.net_g
                else None
            ),
            value=(
                round(self.per_kg * max(0.0, self.remaining_g) / 1000, 2)
                if self.per_kg is not None and self.remaining_g is not None
                else None
            ),
            status=self.status(default_low),
        )
        return d


class FilamentSettings(_M):
    track_usage: bool = True  # charge finished and failed loops to spools
    hold_when_short: bool = True  # keep a loop-end pause waiting when a spool can't cover the next loop
    low_g: float | None = Field(None, ge=0)  # default low-stock warning for spools without their own
    spoolman_url: str | None = None
    spoolman_report: bool = False  # send usage and weigh-ins to Spoolman for imported spools

    @field_validator("spoolman_url")
    @classmethod
    def _url(cls, v):
        v = (v or "").strip().rstrip("/")
        if v and not re.match(r"^https?://", v):
            raise ValueError("Spoolman address must start with http:// or https://")
        return v or None


class Inventory:
    def __init__(self, store, settings=lambda: FilamentSettings(), emit=None, spoolman=None):
        self.store = store
        self.settings = settings
        self._emit = emit or (lambda level, kind, msg, printer=None: None)
        self.spoolman = spoolman  # callable -> client or None

    # ---- spools ---------------------------------------------------------------------
    def all(self, archived: bool = True) -> list[Spool]:
        out = [Spool.model_validate(d) for d in self.store.spools()]
        return out if archived else [s for s in out if not s.archived]

    def get(self, sid: str) -> Spool:
        for s in self.all():
            if s.id == sid:
                return s
        raise KeyError(f"no spool {sid!r}")

    def save(self, spool: Spool | dict, sealed: bool = False) -> Spool:
        """``sealed``: you said the spool is unopened, so it holds what it was sold with."""
        sp = spool if isinstance(spool, Spool) else Spool.model_validate(spool)
        if not sp.id:
            sp.id = secrets.token_hex(4)
        if sealed and sp.net_g:
            sp.remaining_g = sp.net_g
        if not sp.added:
            sp.added = time.time()
        self.store.put_spool(sp.model_dump())
        return sp

    def delete(self, sid: str) -> None:
        self.get(sid)
        self.store.delete_spool(sid)

    # ---- usage ----------------------------------------------------------------------
    def use(
        self,
        spool_id: str | None,
        grams: float,
        kind: str,
        printer: str | None = None,
        job: str | None = None,
        loop_no: int | None = None,
        slot: str | None = None,
        material: str = "",
        color: str = "",
        note: str = "",
    ) -> int:
        uid = self.store.add_usage(
            spool_id=spool_id,
            grams=round(grams, 3),
            kind=kind,
            printer=printer,
            job=job,
            loop_no=loop_no,
            slot=slot,
            material=material,
            color=color,
            note=note,
        )
        if spool_id:
            self._apply(spool_id, grams, kind)
        return uid

    def _apply(self, spool_id: str, grams: float, kind: str) -> None:
        try:
            sp = self.get(spool_id)
        except KeyError:
            return
        before = sp.remaining_g
        if before is not None:
            sp.remaining_g = round(before - grams, 3)
        if grams > 0:
            sp.last_used = time.time()
            sp.opened = sp.opened or sp.last_used
        self.store.put_spool(sp.model_dump())
        self._after_change(sp, before, grams if kind in ("loop", "partial", "manual") else None)

    def _after_change(self, sp: Spool, before: float | None, used: float | None) -> None:
        low = sp.low_g if sp.low_g is not None else self.settings().low_g
        now = sp.remaining_g
        if before is not None and now is not None:
            if now <= 0 < before:
                self._emit("warning", "filament_empty", f"{sp.label} is used up by ProofLoop's count")
            elif low is not None and now <= low < before:
                self._emit("warning", "filament_low", f"{sp.label} is low: {now:.0f} g left")
        sm = self.spoolman() if self.spoolman else None
        if sm and sp.spoolman_id is not None:
            if used is not None and used > 0:
                sm.use_later(sp.spoolman_id, used, self._emit)
            elif now is not None:
                sm.set_remaining_later(sp.spoolman_id, max(0.0, now), self._emit)

    def undo(self, uid: int) -> None:
        row = self.store.usage_row(uid)
        if not row:
            raise KeyError(f"no usage entry {uid}")
        if row["spool_id"]:
            self._apply(row["spool_id"], -row["grams"], "undo")
        self.store.delete_usage(uid)

    def assign_usage(self, uid: int, spool_id: str) -> None:
        row = self.store.usage_row(uid)
        if not row:
            raise KeyError(f"no usage entry {uid}")
        if row["spool_id"]:
            raise ValueError("that usage is already charged to a spool; undo it first")
        self.get(spool_id)
        self.store.set_usage_spool(uid, spool_id)
        self._apply(spool_id, row["grams"], row["kind"])

    def weigh(self, sid: str, gross_g: float) -> Spool:
        sp = self.get(sid)
        if sp.tare_g is None:
            raise ValueError("enter the empty spool's weight first (weigh an empty one of the same kind)")
        new = round(gross_g - sp.tare_g, 1)
        if new < 0:
            raise ValueError(f"that is lighter than the empty spool ({sp.tare_g:g} g)")
        return self.set_remaining(sid, new, f"weighed {gross_g:g} g on a scale", kind="weigh")

    def set_remaining(self, sid: str, grams: float, note: str = "", kind: str = "set") -> Spool:
        sp = self.get(sid)
        before = sp.remaining_g
        self.store.add_usage(
            spool_id=sid,
            grams=round((before - grams) if before is not None else 0.0, 3),
            kind=kind,
            note=note or f"set to {grams:g} g",
            material=sp.material,
            color=sp.color,
        )
        sp.remaining_g = round(grams, 3)
        self.store.put_spool(sp.model_dump())
        self._after_change(sp, before, None)
        return sp

    # ---- numbers ---------------------------------------------------------------------
    def rate(self, spool: Spool | None = None, material: str | None = None, days: int = 14) -> float | None:
        """Grams a day used over the last ``days`` days, from the usage log; None if nothing was used."""
        rows = self.store.usage(time.time() - days * DAY, limit=100000, spool_id=spool.id if spool else None)
        used = sum(
            r["grams"]
            for r in rows
            if r["kind"] in ("loop", "partial", "manual") and (not material or r["material"] == material)
        )
        return used / days if used > 0 else None

    def daily(self, days: int = 30) -> list[dict]:
        start = day_start(time.time()) - (days - 1) * DAY
        out = [{"day": start + i * DAY, "by_material": {}} for i in range(days)]
        for r in self.store.usage(start, limit=100000):
            if r["kind"] not in ("loop", "partial", "manual"):
                continue
            i = int((r["ts"] - start) // DAY)
            if 0 <= i < days:
                m = r["material"] or "?"
                out[i]["by_material"][m] = round(out[i]["by_material"].get(m, 0) + r["grams"], 2)
        return out

    def summary(self) -> dict:
        low = self.settings().low_g
        spools = self.all(archived=False)
        now = time.time()
        used = {d: 0.0 for d in (1, 7, 30)}
        cost = {d: 0.0 for d in (1, 7, 30)}
        by_id = {s.id: s for s in self.all()}
        for r in self.store.usage(now - 30 * DAY, limit=100000):
            if r["kind"] not in ("loop", "partial", "manual"):
                continue
            sp = by_id.get(r["spool_id"] or "")
            for d in used:
                if r["ts"] >= now - d * DAY:
                    used[d] += r["grams"]
                    if sp and sp.per_kg is not None:
                        cost[d] += r["grams"] / 1000 * sp.per_kg
        known = [s for s in spools if s.remaining_g is not None]
        return {
            "spools": len(spools),
            "on_hand_g": round(sum(max(0.0, s.remaining_g) for s in known), 1),
            "unknown_remaining": len(spools) - len(known),
            "value": round(
                sum(s.per_kg * max(0.0, s.remaining_g) / 1000 for s in known if s.per_kg is not None), 2
            ),
            "low": [s.id for s in spools if s.status(low) in ("low", "empty")],
            "used_g": {str(k): round(v, 1) for k, v in used.items()},
            "used_cost": {str(k): round(v, 2) for k, v in cost.items()},
            "unmatched": len(self.store.usage(0, limit=1000, unmatched=True)),
        }

    def forecast(self, sp: Spool) -> dict:
        r = self.rate(sp)
        days = sp.remaining_g / r if r and sp.remaining_g is not None and sp.remaining_g > 0 else None
        return {
            "g_per_day": None if r is None else round(r, 1),
            "days_left": None if days is None else round(days, 1),
        }


def day_start(ts: float) -> float:
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


# ---- which spool feeds which filament slot -----------------------------------------------
def matching_trays(trays: list[dict], material: str, color: str) -> list[dict]:
    m, c = material.upper(), norm_color(color)
    return [t for t in trays if not t.get("empty") and t.get("type", "").upper() == m and t.get("color") == c]


def resolve(
    inv: Inventory,
    printer: str | None,
    slots: list[tuple[str, str, str]],
    trays: list[dict] | None = None,
    tray_hint: int | None = None,
    spools: list[Spool] | None = None,
) -> dict[str, dict]:
    """For each slot: the spool it draws from, the AMS tray, and how that was decided."""
    spools = inv.all(archived=False) if spools is None else spools
    by_tray = {(s.printer, s.tray): s for s in spools if s.printer and s.tray is not None}
    by_id = {s.id: s for s in spools}
    saved = inv.store.assignments()
    trays = trays or []
    out: dict[str, dict] = {}
    for sid, material, color in slots:
        key = slot_key(material, color)
        tray, how, spool = None, "", None
        match = matching_trays(trays, material, color)
        if tray_hint is not None and len(slots) == 1:
            tray, how = tray_hint, "the AMS tray the printer fed from"
        elif len(match) == 1:
            tray, how = match[0]["tray"], "the only AMS tray with this type and colour"
        elif len(match) > 1:
            how = f"{len(match)} AMS trays have this type and colour"
        if tray is not None and printer:
            spool = by_tray.get((printer, tray))
            if spool is None:
                how += "; no spool is linked to that tray"
        if spool is None:
            pick = saved.get((printer or "", key)) or saved.get(("", key))
            if pick in by_id:
                spool, how = by_id[pick], "your choice for this filament"
        if spool is None:
            same = [
                s
                for s in spools
                if s.material == material.upper()
                and s.color == norm_color(color)
                and (s.remaining_g is None or s.remaining_g > 0)
            ]
            if len(same) == 1:
                spool, how = same[0], "the only spool of this type and colour in stock"
            elif len(same) > 1 and not how:
                how = f"{len(same)} spools match: pick one"
        out[sid] = {
            "slot": sid,
            "material": material,
            "color": norm_color(color),
            "key": key,
            "spool": spool.id if spool else None,
            "tray": tray,
            "how": how or "no spool of this type and colour",
            "trays": [t["tray"] for t in match],
        }
    return out


def available(
    inv: Inventory, printer: str | None, pick: dict, spools: list[Spool]
) -> tuple[float | None, list[str]]:
    """Grams the printer can draw for one slot: the picked spool, plus any other linked spool in a
    matching AMS tray (the AMS switches to it when the first runs out). None if any part is unknown."""
    by_id = {s.id: s for s in spools}
    ids = [pick["spool"]] if pick["spool"] else []
    if printer:
        ids += [s.id for s in spools if s.printer == printer and s.tray in pick["trays"] and s.id not in ids]
    if not ids:
        return None, []
    vals = [by_id[i].remaining_g for i in ids if i in by_id]
    if not vals or any(v is None for v in vals):
        return None, ids
    return sum(max(0.0, v) for v in vals), ids


def requirements(
    inv: Inventory,
    printer: str | None,
    slots: list[tuple[str, str, str]],
    slot_grams: dict[str, float],
    loops: int,
    trays: list[dict] | None = None,
    presets: dict[str, dict] | None = None,
) -> dict:
    spools = inv.all(archived=False)
    by_id = {s.id: s for s in spools}
    picks = resolve(inv, printer, slots, trays, spools=spools)
    rows, limit = [], None
    for sid, _m, _c in slots:
        p = picks[sid]
        per = slot_grams.get(sid, 0.0)
        avail, ids = available(inv, printer, p, spools)
        can = math.floor(avail / per + 1e-9) if avail is not None and per > 0 else None
        if can is not None:
            limit = can if limit is None else min(limit, can)
        sp = by_id.get(p["spool"] or "")
        rows.append(
            {
                **p,
                "grams_per_loop": round(per, 2),
                "grams_total": round(per * loops, 1),
                "spool_label": sp.label if sp else None,
                "remaining_g": sp.remaining_g if sp else None,
                "available_g": None if avail is None else round(avail, 1),
                "spools_counted": ids,
                "loops_possible": can,
                "short": can is not None and can < loops,
                "preset": (presets or {}).get(sid, {}),
            }
        )
    return {"loops": loops, "slots": rows, "max_loops": limit, "short": any(r["short"] for r in rows)}


def prices_for(
    inv: Inventory, printer: str | None, slots, trays: list[dict] | None = None
) -> dict[str, dict]:
    """Slot id -> {"spool", "name", "per_kg"} for the cost report; {} where the price isn't known."""
    picks = resolve(inv, printer, slots, trays)
    spools = {sp.id: sp for sp in inv.all()}
    out: dict[str, dict] = {}
    for sid, p in picks.items():
        sp = spools.get(p["spool"] or "")
        if sp:
            out[sid] = {"spool": sp.id, "name": sp.label, "per_kg": sp.per_kg}
            continue
        # Several spools could feed it; if they all cost the same per kg, the price is known anyway.
        same = [
            s
            for s in spools.values()
            if not s.archived and s.material == p["material"].upper() and s.color == p["color"]
        ]
        if same and all(s.per_kg is not None for s in same) and len({round(s.per_kg, 6) for s in same}) == 1:
            out[sid] = {
                "spool": None,
                "name": f"any of {len(same)} matching spools",
                "per_kg": same[0].per_kg,
            }
        else:
            out[sid] = {}
    return out
