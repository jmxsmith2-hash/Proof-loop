"""Batch: several plates in one loop file."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from proofloop.app.routes.common import (
    Guard,
    part_push,
    plate_cost,
    plate_settings,
    profile_or_404,
    send_made,
    with_push,
)
from proofloop.loopfiles.gcode.processor import (
    ProcessError,
    analyze,
    compatibility,
    merged_slots,
    process_batch,
)
from proofloop.monitor.service import App
from proofloop.monitor.tracking import register_job


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    def batch_items(body: dict):
        """(plate, count, that plate's own per-part push heights) in print order."""
        items = []
        for it in body.get("items", []):
            try:
                items.append((app.uploads.get(str(it["token"])), int(it.get("count", 1)), it))
            except (KeyError, TypeError, ValueError) as e:
                raise HTTPException(
                    422, "each batch item needs a token from an uploaded plate and a count"
                ) from e
        items = [(sf, n, part_push(it.get("part_push"))) for sf, n, it in items]
        if not items:
            raise HTTPException(422, "add plates to the batch first")
        return items

    def batch_notes(items) -> list[str]:
        notes = []
        plates = list({id(sf): sf for sf, _, _ in items}.values())
        if len(plates) > 1:
            notes.append(
                "The printer's screen and Bambu Studio show the first plate's picture, print time and filament "
                "for the whole file, as with the FarmLoop app's multi-loop files. ProofLoop's printer monitor follows "
                "each plate."
            )
            seen: dict[str, str] = {}
            shared = set()
            for sf in plates:
                for label in sf.info.object_heights or {}:
                    if label in seen and seen[label] != sf.name:
                        shared.add(label)
                    seen.setdefault(label, sf.name)
            if shared:
                notes.append(
                    f"Some plates use the same object ids ({', '.join(sorted(shared)[:6])}). Don't use "
                    "'skip object' on the printer for this file: it would skip parts on the other plates too."
                )
        return notes

    def batch_cost(items, prof, st, body: dict) -> dict:
        costs = [plate_cost(app, sf, prof, st, n, body, setup=False) for sf, n, _ in items]
        c = app.settings.costs
        setup = (
            c.labor_min_per_run / 60 * c.labor_rate
            if c.labor_rate is not None and c.labor_min_per_run is not None else 0
        )
        total = sum(x["totals"]["run"] for x in costs) + setup
        parts = sum(x["totals"]["parts"] for x in costs)
        return {
            "currency": c.currency,
            "run": round(total, 4),
            "per_part": round(total / max(1, parts), 4),
            "complete": all(x["totals"]["complete"] for x in costs),
        }

    @api.post("/api/batch/preview", dependencies=guard)
    def batch_preview(body: dict):
        items = batch_items(body)
        defaults, st = plate_settings(app, body.get("settings"))
        prof = profile_or_404(app, defaults.profile_id)
        first = items[0][0]
        rows: list[dict] = []
        problems: list[str] = []
        earlier: list = []
        for sf, n, pp in items:
            row = {
                "file": sf.name,
                "count": n,
                "parts": 0,
                "print_seconds": sf.info.print_seconds,
                "problems": [],
            }
            if sf is not first and all(sf is not e for e in earlier):
                row["problems"] += compatibility(first, sf, earlier)
            if all(sf is not e for e in earlier):
                earlier.append(sf)
            try:
                a = analyze(sf, prof, with_push(st, pp))
                row["parts"] = len(a.parts)
                row["custom_heights"] = len(a.plan.part_custom)
                row["problems"] += a.problems
                row["sweep_s"] = round(a.sweep_s)
            except ProcessError as e:
                row["problems"].append(str(e))
            problems += row["problems"]
            rows.append(row)
        return {
            "items": rows,
            "notes": batch_notes(items),
            "filaments": [{"slot": s, "type": t, "color": c} for s, t, c in merged_slots([sf for sf, _, _ in items])],
            "loops": sum(n for _, n, _ in items),
            "parts": sum(r["parts"] * r["count"] for r in rows),
            "print_seconds": sum(r["print_seconds"] * r["count"] for r in rows),
            "ok": not problems,
            "cost": batch_cost(items, prof, st, body) if not problems else None,
        }

    def made_batch(body: dict):
        """The batch file, recorded (for filament counting and the journal) before it leaves ProofLoop."""
        items = batch_items(body)
        defaults, st = plate_settings(app, body.get("settings"))
        prof = profile_or_404(app, defaults.profile_id)
        r = process_batch(items, prof, st)  # a ProcessError is answered by loop_server's handler
        register_job(
            app.store,
            r.filename,
            [(sf, n, len(analyze(sf, prof, with_push(st, pp)).parts)) for sf, n, pp in items],
            prof.id,
        )
        app.journal.keep_file(r.filename, r.data)
        return r

    @api.post("/api/batch/printers", dependencies=guard)
    def batch_targets(body: dict):
        """Which printers the batch could go to now: every filament of every plate must be loaded."""
        if not allow_send:
            raise HTTPException(403, "files can't be sent to printers from here")
        items = batch_items(body)
        slots = merged_slots([sf for sf, _, _ in items])
        run = [sf.info.slot_grams for sf, n, _ in items for _ in range(n)]
        return {"send_to_printer": app.settings.send_to_printer,
                "printers": app.send_readiness(items[0][0], slots, run)}

    @api.post("/api/batch/send", dependencies=guard)
    def batch_send(body: dict):
        """Make the batch file, as Download does, and send it to a printer you named."""
        name, confirm = str(body.get("printer", "") or ""), body.get("confirm")
        if not allow_send:
            raise HTTPException(403, "files can't be sent to printers from here")
        if not name or confirm != name:
            raise HTTPException(400, "confirm the printer to send it to")
        return send_made(app, allow_send, name, confirm, made_batch(body))

    @api.post("/api/batch/download", dependencies=guard)
    def batch_download(body: dict):
        r = made_batch(body)
        return Response(
            r.data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{r.filename}"',
                "X-ProofLoop-Filename": r.filename,
            },
        )
