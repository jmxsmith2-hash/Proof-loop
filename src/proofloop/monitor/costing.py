"""What a part really costs, and what it earns.

ProofLoop never invents a price. Every rate here (filament, electricity,
printer, plates, labour, fees...) is one you enter. Every quantity comes from
the file (grams per filament slot, print time, parts per plate), from the
hardware profile (FarmBoard waits, sweep time), or from what the Monitor
measured on your printer (cooldown time). A cost you haven't set is reported
as missing and left out of the totals instead of being guessed, so the total
is always "at least this much", with the gaps listed.

Model, per loop (one plate printed, cooled, ejected):

* material   = sum over slots of grams x (spool price / spool weight)
* energy     = (print hours x printing W + cooldown/eject hours x idle W) / 1000 x price per kWh
* machine    = loop hours x (printer price / expected life hours + maintenance per hour)
* plate wear = plate price / loops per plate
* kit wear   = FarmLoop kit price / loops the kit lasts
* labour     = minutes per loop / 60 x hourly rate
* overhead   = loop hours x monthly overhead / printer hours per month
* failures   = all of the above divided by (1 - failure rate): failed loops cost the same and make nothing
* extras     = per-part extras (packaging, inserts...) x parts on the plate (good parts only)

A run adds one-off setup labour. Selling subtracts tax included in the price,
marketplace and payment fees, and per-order costs (shipping, boxes) spread over
the parts in an order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Spool(_M):
    name: str = ""
    type: str = ""  # PLA, PETG... matched against the slicer's filament type
    color: str = ""  # #RRGGBB, optional; matched against the slicer's colour
    price: float = Field(ge=0)
    weight_g: float = Field(1000, gt=0)

    @property
    def per_kg(self) -> float:
        return self.price / (self.weight_g / 1000)


class Extra(_M):
    name: str
    cost: float = Field(ge=0)


class CostSettings(_M):
    currency: str = "$"
    spools: list[Spool] = Field(default_factory=list)
    slot_spool: dict[str, int] = Field(default_factory=dict)  # "TYPE|#COLOR" -> spool index, set in the app
    electricity_per_kwh: float | None = Field(None, ge=0)
    watts_printing: float | None = Field(None, ge=0)  # measure with a smart plug
    watts_idle: float | None = Field(None, ge=0)  # cooling and ejecting, bed off
    printer_price: float | None = Field(None, ge=0)
    printer_life_hours: float | None = Field(None, gt=0)
    maintenance_per_hour: float | None = Field(None, ge=0)  # nozzles, rods, belts... spread per hour
    plate_price: float | None = Field(None, ge=0)
    plate_life_loops: float | None = Field(None, gt=0)
    kit_price: float | None = Field(None, ge=0)
    kit_life_loops: float | None = Field(None, gt=0)
    labor_rate: float | None = Field(None, ge=0)  # per hour
    labor_min_per_loop: float | None = Field(None, ge=0)  # emptying the bin, checking parts
    labor_min_per_run: float | None = Field(None, ge=0)  # slicing, loading filament, starting
    overhead_per_month: float | None = Field(None, ge=0)  # rent, software, insurance
    printer_hours_per_month: float | None = Field(None, gt=0)
    failure_rate_pct: float = Field(0, ge=0, lt=95)
    extras_per_part: list[Extra] = Field(default_factory=list)
    extras_per_order: list[Extra] = Field(default_factory=list)
    # selling
    price_per_part: float | None = Field(None, ge=0)
    parts_per_order: float = Field(1, gt=0)
    tax_included_pct: float = Field(0, ge=0, lt=100)  # VAT / sales tax included in your price
    marketplace_fee_pct: float = Field(0, ge=0, lt=100)
    marketplace_fee_per_order: float = Field(0, ge=0)
    payment_fee_pct: float = Field(0, ge=0, lt=100)
    payment_fee_per_order: float = Field(0, ge=0)
    shipping_cost_per_order: float = Field(0, ge=0)
    shipping_charged_per_order: float = Field(0, ge=0)
    target_margin_pct: float = Field(30, ge=0, lt=100)
    # capacity
    printers: int = Field(1, ge=1, le=1000)
    hours_per_day: float = Field(24, gt=0, le=24)
    days_per_month: float = Field(30, gt=0, le=31)


@dataclass
class Times:
    """Seconds per loop, each with where it came from."""

    print_s: float
    fixed_s: float  # FarmBoard waits + sweep, from the profile and plan
    cooldown_s: float | None  # measured on your printer, or your estimate; None = unknown
    cooldown_source: str = ""

    @property
    def known_s(self) -> float:
        return self.print_s + self.fixed_s + (self.cooldown_s or 0)


@dataclass
class Line:
    key: str
    label: str
    per_loop: float | None  # None = not set
    how: str
    missing: str = ""


@dataclass
class CostReport:
    currency: str
    parts_per_plate: int
    loops: int
    lines: list[Line] = field(default_factory=list)
    setup: float | None = None
    times: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    selling: dict | None = None
    capacity: dict | None = None
    missing: list[str] = field(default_factory=list)
    materials: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "currency": self.currency,
            "parts_per_plate": self.parts_per_plate,
            "loops": self.loops,
            "lines": [ln.__dict__ for ln in self.lines],
            "setup": self.setup,
            "times": self.times,
            "totals": self.totals,
            "selling": self.selling,
            "capacity": self.capacity,
            "missing": self.missing,
            "materials": self.materials,
        }


def slot_key(ftype: str, color: str) -> str:
    return f"{ftype.upper()}|{color.upper()}"


def match_spool(c: CostSettings, ftype: str, color: str) -> int | None:
    """Your choice first; then a spool with the same type and colour; then the only spool of that type."""
    key = slot_key(ftype, color)
    if key in c.slot_spool and 0 <= c.slot_spool[key] < len(c.spools):
        return c.slot_spool[key]
    same = [
        i
        for i, s in enumerate(c.spools)
        if s.type.upper() == ftype.upper() and s.color and s.color.upper() == color.upper()
    ]
    if same:
        return same[0]
    of_type = [i for i, s in enumerate(c.spools) if s.type.upper() == ftype.upper()]
    return of_type[0] if len(of_type) == 1 else None


def _r(v: float | None, n: int = 4) -> float | None:
    return None if v is None else round(v, n)


def cost(
    c: CostSettings,
    slots: list[tuple[str, str, str]],
    slot_grams: dict[str, float],
    parts_per_plate: int,
    times: Times,
    loops: int = 1,
    prices: dict[str, dict] | None = None,
) -> CostReport:
    """``prices``: slot id -> {"spool", "name", "per_kg"} from the filament inventory. Without it the
    older price list in ``c.spools`` is used."""
    rep = CostReport(c.currency, parts_per_plate, loops)
    cur = c.currency
    hours = times.known_s / 3600
    print_h, rest_h = times.print_s / 3600, (times.fixed_s + (times.cooldown_s or 0)) / 3600
    rep.times = {
        "print_s": round(times.print_s),
        "fixed_s": round(times.fixed_s),
        "cooldown_s": None if times.cooldown_s is None else round(times.cooldown_s),
        "cooldown_source": times.cooldown_source,
        "loop_s": round(times.known_s),
    }

    def add(key, label, value, how, missing=""):
        rep.lines.append(Line(key, label, _r(value), how, missing))
        if missing:
            rep.missing.append(missing)

    # material, slot by slot
    mat_total, mat_missing = 0.0, []
    for sid, ftype, color in slots:
        g = slot_grams.get(sid, 0.0)
        if prices is not None:
            pr = prices.get(sid) or {}
            row = {
                "slot": sid,
                "type": ftype,
                "color": color,
                "grams": round(g, 2),
                "spool": pr.get("spool"),
                "spool_name": pr.get("name"),
                "cost": None,
                "key": slot_key(ftype, color),
            }
            if pr.get("per_kg") is not None:
                row.update(per_kg=round(pr["per_kg"], 2), cost=round(g / 1000 * pr["per_kg"], 4))
                mat_total += g / 1000 * pr["per_kg"]
            else:
                mat_missing.append(f"{ftype} {color}".strip())
            rep.materials.append(row)
            continue
        i = match_spool(c, ftype, color)
        row = {
            "slot": sid,
            "type": ftype,
            "color": color,
            "grams": round(g, 2),
            "spool": None,
            "cost": None,
            "key": slot_key(ftype, color),
        }
        if i is not None:
            sp = c.spools[i]
            row.update(
                spool=i,
                spool_name=sp.name or f"{sp.type} {sp.color}",
                per_kg=round(sp.per_kg, 2),
                cost=round(g / 1000 * sp.per_kg, 4),
            )
            mat_total += g / 1000 * sp.per_kg
        else:
            mat_missing.append(f"{ftype} {color}".strip())
        rep.materials.append(row)
    add(
        "material",
        "Filament",
        mat_total if not mat_missing else (mat_total or None),
        f"{sum(slot_grams.values()):.1f} g from the slicer, priced from your spools",
        f"pick a spool with a price and weight for {', '.join(mat_missing)}" if mat_missing else "",
    )

    if None in (c.electricity_per_kwh, c.watts_printing):
        add(
            "energy",
            "Electricity",
            None,
            "printer watts x hours x price per kWh",
            "set the electricity price and the printer's power",
        )
    else:
        idle = c.watts_idle if c.watts_idle is not None else c.watts_printing
        e = (print_h * c.watts_printing + rest_h * idle) / 1000 * c.electricity_per_kwh
        add(
            "energy",
            "Electricity",
            e,
            f"{print_h:.2f} h at {c.watts_printing:g} W + {rest_h:.2f} h at {idle:g} W, {cur}{c.electricity_per_kwh:g}/kWh",
        )

    if c.printer_price is None or c.printer_life_hours is None:
        add(
            "machine",
            "Printer wear",
            None,
            "printer price / expected life hours",
            "set the printer price and its expected life in hours",
        )
    else:
        per_h = c.printer_price / c.printer_life_hours
        add("machine", "Printer wear", hours * per_h, f"{hours:.2f} h x {cur}{per_h:.4g}/h")
    if c.maintenance_per_hour is not None:
        add(
            "maintenance",
            "Maintenance",
            hours * c.maintenance_per_hour,
            f"{hours:.2f} h x {cur}{c.maintenance_per_hour:g}/h",
        )
    if c.plate_price is not None and c.plate_life_loops is not None:
        add(
            "plate",
            "Build plate wear",
            c.plate_price / c.plate_life_loops,
            f"{cur}{c.plate_price:g} over {c.plate_life_loops:g} loops (the Monitor counts bends)",
        )
    else:
        add(
            "plate",
            "Build plate wear",
            None,
            "plate price / loops per plate",
            "set the plate price and how many loops it lasts",
        )
    if c.kit_price is not None and c.kit_life_loops is not None:
        add(
            "kit",
            "FarmLoop kit wear",
            c.kit_price / c.kit_life_loops,
            f"{cur}{c.kit_price:g} over {c.kit_life_loops:g} loops",
        )
    if c.labor_rate is not None and c.labor_min_per_loop is not None:
        add(
            "labor",
            "Labour per loop",
            c.labor_min_per_loop / 60 * c.labor_rate,
            f"{c.labor_min_per_loop:g} min at {cur}{c.labor_rate:g}/h",
        )
    if c.overhead_per_month is not None and c.printer_hours_per_month is not None:
        add(
            "overhead",
            "Overhead",
            hours * c.overhead_per_month / c.printer_hours_per_month,
            f"{cur}{c.overhead_per_month:g}/month over {c.printer_hours_per_month:g} printer hours",
        )
    if times.cooldown_s is None:
        rep.missing.append(
            "cooldown time unknown: time-based costs leave out the cooldown (the Monitor measures it)"
        )

    production = sum(ln.per_loop or 0 for ln in rep.lines)
    f = c.failure_rate_pct / 100
    if f:
        add(
            "failures",
            "Failed loops",
            production / (1 - f) - production,
            f"{c.failure_rate_pct:g}% of loops fail and are reprinted",
        )
    extras = sum(e.cost for e in c.extras_per_part) * parts_per_plate
    if c.extras_per_part:
        add(
            "extras",
            "Per-part extras",
            extras,
            ", ".join(f"{e.name} {cur}{e.cost:g}" for e in c.extras_per_part) + f" x {parts_per_plate} parts",
        )

    per_loop = sum(ln.per_loop or 0 for ln in rep.lines)
    if c.labor_rate is not None and c.labor_min_per_run is not None:
        rep.setup = round(c.labor_min_per_run / 60 * c.labor_rate, 4)
    run = per_loop * loops + (rep.setup or 0)
    parts = max(1, parts_per_plate * loops)
    rep.totals = {
        "per_loop": round(per_loop, 4),
        "per_part": round(run / parts, 4),
        "run": round(run, 4),
        "parts": parts,
        "per_hour": round(per_loop / hours, 4) if hours else None,
        "complete": not rep.missing,
    }

    rep.selling = _selling(c, run / parts) if c.price_per_part is not None else None
    if times.cooldown_s is not None and times.known_s > 0:
        rep.capacity = _capacity(c, times.known_s, parts_per_plate, rep.selling)
    return rep


def _per_order_fixed(c: CostSettings) -> float:
    order = (
        c.marketplace_fee_per_order
        + c.payment_fee_per_order
        + c.shipping_cost_per_order
        - c.shipping_charged_per_order
        + sum(e.cost for e in c.extras_per_order)
    )
    return order / c.parts_per_order


def _net_share(c: CostSettings) -> float:
    """What's left of each unit of price after included tax and percentage fees."""
    return 1 / (1 + c.tax_included_pct / 100) - (c.marketplace_fee_pct + c.payment_fee_pct) / 100


def _selling(c: CostSettings, cost_per_part: float) -> dict:
    price = c.price_per_part
    tax = price - price / (1 + c.tax_included_pct / 100)
    pct_fees = price * (c.marketplace_fee_pct + c.payment_fee_pct) / 100
    fixed = _per_order_fixed(c)
    profit = price - tax - pct_fees - fixed - cost_per_part
    share = _net_share(c)
    breakeven = (cost_per_part + fixed) / share if share > 0 else None
    m = c.target_margin_pct / 100
    target = (cost_per_part + fixed) / (share - m) if share - m > 0 else None
    return {
        "price": round(price, 4),
        "tax": round(tax, 4),
        "percent_fees": round(pct_fees, 4),
        "per_order_costs": round(fixed, 4),
        "cost": round(cost_per_part, 4),
        "profit": round(profit, 4),
        "margin_pct": round(profit / price * 100, 2) if price else None,
        "markup_pct": round(profit / cost_per_part * 100, 2) if cost_per_part else None,
        "breakeven_price": _r(breakeven),
        "target_margin_pct": c.target_margin_pct,
        "price_for_target": _r(target),
    }


def _capacity(c: CostSettings, loop_s: float, parts_per_plate: int, selling: dict | None) -> dict:
    loops_day = c.hours_per_day * 3600 / loop_s * c.printers
    good = 1 - c.failure_rate_pct / 100
    parts_day = loops_day * parts_per_plate * good
    out = {
        "loops_per_day": round(loops_day, 2),
        "parts_per_day": round(parts_day, 1),
        "parts_per_month": round(parts_day * c.days_per_month),
        "printers": c.printers,
    }
    if selling:
        out["profit_per_day"] = round(parts_day * selling["profit"], 2)
        out["profit_per_month"] = round(parts_day * c.days_per_month * selling["profit"], 2)
        out["revenue_per_month"] = round(parts_day * c.days_per_month * selling["price"], 2)
    return out


def loops_for(quantity: int, parts_per_plate: int) -> int:
    return math.ceil(quantity / max(1, parts_per_plate))
