"""The Overview: what was made, what is running, what is low."""

from __future__ import annotations

import time

from fastapi import FastAPI

from proofloop.app.routes.common import Guard
from proofloop.app.routes.filament import spool_out
from proofloop.monitor.filament import DAY, day_start
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    inv = app.inventory

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
        per_printer: dict[str, int] = {}
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
                spool_out(app, s)
                for s in inv.all(archived=False)
                if s.status(app.settings.filament.low_g) in ("low", "empty")
            ],
            "events": app.store.events(8),
        }
