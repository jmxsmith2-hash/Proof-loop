"""The filament inventory: spools, what each print used, weigh-ins, links to AMS trays, Spoolman."""

from __future__ import annotations

import csv
import io
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from proofloop.app.routes.common import Guard, trays_of
from proofloop.monitor.exports import safe  # labels and notes can't become formulas
from proofloop.monitor.filament import (
    DAY,
    FilamentSettings,
    Spool,
    norm_color,
    requirements,
)
from proofloop.monitor.service import App
from proofloop.monitor.spoolman import SpoolmanError, to_spool


def spool_out(app: App, sp: Spool) -> dict:
    d = sp.as_dict(app.settings.filament.low_g)
    d["forecast"] = app.inventory.forecast(sp)
    return d


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    inv = app.inventory

    def spool_or_404(sid: str) -> Spool:
        try:
            return inv.get(sid)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    def bad(e: Exception):
        return HTTPException(422, str(e))

    def printers_ams() -> list[dict]:
        out = []
        for w in app.watches.values():
            st = w.link.state
            out.append(
                {
                    "name": w.name,
                    "online": st.online,
                    "units": st.ams.units_dict(),
                    "trays": st.ams.trays(),
                    "tray_now": st.ams.tray_now,
                }
            )
        return out

    @api.get("/api/filament", dependencies=guard)
    def filament():
        spools = inv.all()
        return {
            "spools": [spool_out(app, s) for s in spools],
            "summary": inv.summary(),
            "settings": app.settings.filament.model_dump(),
            "printers": printers_ams(),
            "unmatched": inv.store.usage(0, limit=200, unmatched=True),
            "materials": sorted({s.material for s in spools}),
            "assignments": [
                {"printer": p, "key": k, "spool": v} for (p, k), v in inv.store.assignments().items()
            ],
        }

    @api.post("/api/filament/spools", dependencies=guard)
    def add_spool(body: dict):
        body = dict(body)
        sealed = bool(body.pop("sealed", False))
        copies = int(body.pop("copies", 1) or 1)
        if not 1 <= copies <= 50:
            raise HTTPException(422, "add between 1 and 50 spools at a time")
        body.pop("id", None)
        try:
            made = [inv.save(Spool.model_validate(body), sealed=sealed) for _ in range(copies)]
        except ValueError as e:
            raise bad(e) from e
        return [spool_out(app, s) for s in made]

    @api.put("/api/filament/spools/{sid}", dependencies=guard)
    def edit_spool(sid: str, body: dict):
        old = spool_or_404(sid)
        keep = {"id", "added", "last_used", "printer", "tray"}
        merged = {**old.model_dump(), **{k: v for k, v in body.items() if k not in keep}}
        try:
            return spool_out(app, inv.save(Spool.model_validate(merged)))
        except ValueError as e:
            raise bad(e) from e

    @api.delete("/api/filament/spools/{sid}", dependencies=guard)
    def delete_spool(sid: str):
        spool_or_404(sid)
        inv.delete(sid)
        return {"deleted": sid}

    @api.post("/api/filament/spools/{sid}/weigh", dependencies=guard)
    def weigh(sid: str, body: dict):
        spool_or_404(sid)
        try:
            return spool_out(app, inv.weigh(sid, float(body["gross_g"])))
        except (KeyError, TypeError, ValueError) as e:
            raise bad(e) from e

    @api.post("/api/filament/spools/{sid}/remaining", dependencies=guard)
    def set_remaining(sid: str, body: dict):
        spool_or_404(sid)
        try:
            g = float(body["grams"])
            if g < 0:
                raise ValueError("remaining filament can't be negative")
            return spool_out(app, inv.set_remaining(sid, g, str(body.get("note") or "")))
        except (KeyError, TypeError, ValueError) as e:
            raise bad(e) from e

    @api.post("/api/filament/spools/{sid}/use", dependencies=guard)
    def use(sid: str, body: dict):
        sp = spool_or_404(sid)
        try:
            g = float(body["grams"])
        except (KeyError, TypeError, ValueError) as e:
            raise bad(ValueError("say how many grams")) from e
        if not 0 < g < 100000:
            raise HTTPException(422, "grams must be more than 0")
        inv.use(
            sid,
            g,
            "manual",
            note=str(body.get("note") or "used by hand"),
            material=sp.material,
            color=sp.color,
        )
        return spool_out(app, inv.get(sid))

    @api.post("/api/filament/spools/{sid}/dried", dependencies=guard)
    def dried(sid: str):
        sp = spool_or_404(sid)
        sp.dried = time.time()
        return spool_out(app, inv.save(sp))

    @api.post("/api/filament/link", dependencies=guard)
    def link(body: dict):
        try:
            printer, tray = str(body["printer"]), int(body["tray"])
        except (KeyError, TypeError, ValueError) as e:
            raise bad(ValueError("say which printer and tray")) from e
        if printer not in app.watches:
            raise HTTPException(404, f"no printer {printer!r}")
        sid = body.get("spool") or None
        if sid:
            spool_or_404(sid)
        app.tracker.link(printer, tray, sid)
        return {"ok": True}

    @api.put("/api/filament/assign", dependencies=guard)
    def assign(body: dict):
        key = str(body.get("key") or "")
        if "|" not in key:
            raise HTTPException(422, "key must be TYPE|#COLOUR")
        sid = body.get("spool") or None
        if sid:
            spool_or_404(sid)
        inv.store.assign(str(body.get("printer") or ""), key, sid)
        return {"ok": True}

    @api.get("/api/filament/usage", dependencies=guard)
    def usage(days: int = 30, spool: str | None = None):
        days = max(1, min(days, 366))
        return {
            "rows": inv.store.usage(time.time() - days * DAY, limit=1000, spool_id=spool),
            "daily": inv.daily(min(days, 90)),
        }

    @api.post("/api/filament/usage/{uid}/assign", dependencies=guard)
    def assign_usage(uid: int, body: dict):
        try:
            inv.assign_usage(uid, str(body["spool"]))
        except (KeyError, ValueError) as e:
            raise bad(e) from e
        return {"ok": True}

    @api.delete("/api/filament/usage/{uid}", dependencies=guard)
    def undo_usage(uid: int):
        try:
            inv.undo(uid)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True}

    @api.put("/api/filament/settings", dependencies=guard)
    def filament_settings(body: dict):
        try:
            app.settings.filament = FilamentSettings.model_validate(body)
        except ValueError as e:
            raise bad(e) from e
        app.save_settings()
        return app.settings.filament.model_dump()

    @api.post("/api/filament/spoolman/import", dependencies=guard)
    def spoolman_import():
        sm = app.spoolman()
        if not sm:
            raise HTTPException(422, "enter your Spoolman address first")
        try:
            remote = sm.spools()
        except SpoolmanError as e:
            raise HTTPException(502, str(e)) from e
        mine = {s.spoolman_id: s for s in inv.all() if s.spoolman_id is not None}
        added = updated = 0
        for r in remote:
            try:
                fields = to_spool(r)
            except (KeyError, TypeError, ValueError):
                continue
            old = mine.get(fields["spoolman_id"])
            if old:
                keep = old.model_dump()
                keep.update({k: v for k, v in fields.items() if v not in (None, "")})
                inv.save(Spool.model_validate(keep))
                updated += 1
            else:
                inv.save(Spool.model_validate(fields))
                added += 1
        return {"added": added, "updated": updated}

    @api.get("/api/filament/export.csv", dependencies=guard)
    def export_csv(what: str = "spools"):
        buf = io.StringIO()
        if what == "usage":
            rows = inv.store.usage(0, limit=1000000)
            cols = [
                "id",
                "ts",
                "spool_id",
                "grams",
                "kind",
                "printer",
                "job",
                "loop_no",
                "slot",
                "material",
                "color",
                "note",
            ]
            w = csv.DictWriter(buf, fieldnames=["time", *cols], extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                            **{k: safe(v) for k, v in r.items()}})
        else:
            cols = list(Spool.model_fields) + ["per_kg", "remaining_pct", "value", "status"]
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for s in inv.all():
                w.writerow({k: safe(v) for k, v in s.as_dict(app.settings.filament.low_g).items()})
        name = "proofloop-usage.csv" if what == "usage" else "proofloop-spools.csv"
        return Response(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @api.get("/api/filament/tray-colors", dependencies=guard)
    def tray_colors():
        """Colours currently in the AMS trays, for the spool editor's swatches."""
        out = []
        for p in printers_ams():
            out += [
                {"printer": p["name"], **t}
                for t in p["trays"]
                if not t.get("empty") and norm_color(t.get("color", ""))
            ]
        return out

    @api.post("/api/plates/{token}/filament", dependencies=guard)
    def plate_filament(token: str, body: dict):
        """What filament a plate needs, and whether the spools you have cover it."""
        sf = app.uploads.get(token)
        printer = body.get("printer") or None
        if printer and printer not in app.watches:
            raise HTTPException(404, f"no printer {printer!r}")
        loops = max(1, int(body.get("loops") or app.settings.defaults.loops))
        out = requirements(
            app.inventory,
            printer,
            sf.info.filament_slots,
            sf.info.slot_grams,
            loops,
            trays_of(app, printer),
            sf.info.presets,
        )
        out["printer"] = printer
        return out
