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
    with_push,
)
from proofloop.loopfiles.gcode.processor import ProcessError, analyze, compatibility, process_batch
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
        rows, problems = [], []
        for sf, n, pp in items:
            row = {
                "file": sf.name,
                "count": n,
                "parts": 0,
                "print_seconds": sf.info.print_seconds,
                "problems": [],
            }
            row["problems"] += compatibility(first, sf) if sf is not first else []
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
            "loops": sum(n for _, n, _ in items),
            "parts": sum(r["parts"] * r["count"] for r in rows),
            "print_seconds": sum(r["print_seconds"] * r["count"] for r in rows),
            "ok": not problems,
            "cost": batch_cost(items, prof, st, body) if not problems else None,
        }

    @api.post("/api/batch/download", dependencies=guard)
    def batch_download(body: dict):
        items = batch_items(body)
        defaults, st = plate_settings(app, body.get("settings"))
        prof = profile_or_404(app, defaults.profile_id)
        r = process_batch(items, prof, st)
        register_job(
            app.store,
            r.filename,
            [(sf, n, len(analyze(sf, prof, with_push(st, pp)).parts)) for sf, n, pp in items],
            prof.id,
        )
        app.journal.keep_file(r.filename, r.data)
        return Response(
            r.data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{r.filename}"',
                "X-ProofLoop-Filename": r.filename,
            },
        )
