"""API for the filament inventory, the overview dashboard, and backup/restore."""

from __future__ import annotations

import csv
import io
import json
import time

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response

from . import __version__
from .config import Settings
from .filament import DAY, FilamentSettings, Spool, day_start, norm_color
from .profiles import HardwareProfile
from .spoolman import SpoolmanError, to_spool


def register(api: FastAPI, app, guard) -> None:
    inv = app.inventory

    def spool_or_404(sid: str) -> Spool:
        try:
            return inv.get(sid)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    def bad(e: Exception):
        return HTTPException(422, str(e))

    def spool_out(sp: Spool) -> dict:
        d = sp.as_dict(app.settings.filament.low_g)
        d["forecast"] = inv.forecast(sp)
        return d

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

    # ---- filament ---------------------------------------------------------------------
    @api.get("/api/filament", dependencies=guard)
    def filament():
        spools = inv.all()
        return {
            "spools": [spool_out(s) for s in spools],
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
        return [spool_out(s) for s in made]

    @api.put("/api/filament/spools/{sid}", dependencies=guard)
    def edit_spool(sid: str, body: dict):
        old = spool_or_404(sid)
        keep = {"id", "added", "last_used", "printer", "tray"}
        merged = {**old.model_dump(), **{k: v for k, v in body.items() if k not in keep}}
        try:
            return spool_out(inv.save(Spool.model_validate(merged)))
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
            return spool_out(inv.weigh(sid, float(body["gross_g"])))
        except (KeyError, TypeError, ValueError) as e:
            raise bad(e) from e

    @api.post("/api/filament/spools/{sid}/remaining", dependencies=guard)
    def set_remaining(sid: str, body: dict):
        spool_or_404(sid)
        try:
            g = float(body["grams"])
            if g < 0:
                raise ValueError("remaining filament can't be negative")
            return spool_out(inv.set_remaining(sid, g, str(body.get("note") or "")))
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
        return spool_out(inv.get(sid))

    @api.post("/api/filament/spools/{sid}/dried", dependencies=guard)
    def dried(sid: str):
        sp = spool_or_404(sid)
        sp.dried = time.time()
        return spool_out(inv.save(sp))

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
                w.writerow({"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), **r})
        else:
            cols = list(Spool.model_fields) + ["per_kg", "remaining_pct", "value", "status"]
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for s in inv.all():
                w.writerow(s.as_dict(app.settings.filament.low_g))
        name = "clearloop-usage.csv" if what == "usage" else "clearloop-spools.csv"
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

    # ---- overview -----------------------------------------------------------------------
    @api.get("/api/overview", dependencies=guard)
    def overview(days: int = 14):
        days = max(1, min(days, 90))
        now = time.time()
        prod = app.store.production(now - max(days, 30) * DAY)

        def window(d):
            since = day_start(now) - (d - 1) * DAY  # whole calendar days, today included
            rows = [r for r in prod if r["ts"] >= since]
            return {
                "loops": sum(1 for r in rows if r["ok"]),
                "failed": sum(1 for r in rows if not r["ok"]),
                "parts": sum(r["parts"] for r in rows if r["ok"]),
                "grams": round(sum(r["grams"] for r in rows if r["ok"]), 1),
            }

        start = day_start(now) - (days - 1) * DAY
        daily = []
        for i in range(days):
            a, b = start + i * DAY, start + (i + 1) * DAY
            rows = [r for r in prod if a <= r["ts"] < b and r["ok"]]
            daily.append({"day": a, "parts": sum(r["parts"] for r in rows), "loops": len(rows)})
        runs = []
        for w in app.watches.values():
            st = w.link.state
            _key, job = app.tracker.job_for(st)
            t = app.store.timings(w.name)
            total = job["loops"] if job else None
            eta = None
            if total and "loop" in t and st.gcode_state in ("RUNNING", "PAUSE", "PREPARE"):
                left = max(0, total - w.loop_no)
                eta = (
                    now
                    + left * t["loop"]["avg"]
                    + (st.remaining_min * 60 if st.gcode_state == "RUNNING" else 0)
                )
            runs.append(
                {
                    "printer": w.name,
                    "status": w.status,
                    "phase": w.phase,
                    "online": st.online,
                    "job": st.subtask_name,
                    "loop_no": w.loop_no if job else None,
                    "loops": total,
                    "eta": eta,
                    "eta_basis": f"average of {t['loop']['n']} measured loops" if eta else None,
                    "waiting_for_you": w.waiting_for_you,
                }
            )
        per_printer = {}
        for r in prod:
            if r["ts"] >= now - 7 * DAY and r["ok"]:
                per_printer[r["printer"]] = per_printer.get(r["printer"], 0) + r["parts"]
        return {
            "today": window(1),
            "week": window(7),
            "month": window(30),
            "daily": daily,
            "runs": runs,
            "parts_per_printer_7d": per_printer,
            "filament": inv.summary(),
            "low_spools": [
                spool_out(s)
                for s in inv.all(archived=False)
                if s.status(app.settings.filament.low_g) in ("low", "empty")
            ],
            "events": app.store.events(8),
        }

    # ---- backup -------------------------------------------------------------------------
    @api.get("/api/backup", dependencies=guard)
    def backup():
        user_profiles = [
            json.loads(p.to_json()) for p in app.profiles.all() if p.source.get("kind") != "builtin"
        ]
        data = {
            "clearloop_backup": 1,
            "version": __version__,
            "created": time.time(),
            "settings": json.loads(app.settings.model_dump_json()),
            "profiles": user_profiles,
            "store": app.store.dump(),
        }
        stamp = time.strftime("%Y%m%d-%H%M")
        return Response(
            json.dumps(data),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="clearloop-backup-{stamp}.json"'},
        )

    @api.post("/api/restore", dependencies=guard)
    async def restore(file: UploadFile = File(...)):
        raw = await file.read(200 * 1024 * 1024)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("clearloop_backup") != 1:
                raise ValueError("this isn't a ClearLoop backup file")
            settings = Settings.model_validate(data.get("settings") or {})
            profiles = [HardwareProfile.from_dict(p) for p in data.get("profiles") or []]
        except (ValueError, TypeError, KeyError) as e:
            raise HTTPException(422, f"can't restore: {e}") from e
        app.store.restore(data.get("store") or {})
        for p in profiles:
            try:
                app.profiles.save(p)
            except ValueError:
                pass
        for name in list(app.watches):
            app.remove_printer(name)
        app.settings = settings
        app.save_settings()
        for p in settings.printers:
            app._start_watch(p)
        return {"restored": True, "spools": len(app.inventory.all()), "printers": len(settings.printers)}
