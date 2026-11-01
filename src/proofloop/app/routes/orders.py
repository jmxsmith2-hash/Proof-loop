"""Customer orders: a quantity and a due date, planned as a run in the queue and followed to the end."""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException

from proofloop.app.routes.common import Guard, plan_for, plate_cost, plate_settings, profile_or_404
from proofloop.core import schema
from proofloop.core.settings import Business
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    @api.get("/api/orders", dependencies=guard)
    def list_orders():
        from proofloop.monitor.orders import progress

        runs = {r["id"]: r for r in app.queue.runs()}
        out = [{**progress(app, o), "proofs": app.proofs.for_jobs(runs.get(o["run"], {}).get("jobs", []))}
               for o in app.orders.all()]  # each order's gallery: its finished plates, before the sweep
        return sorted(out, key=lambda o: (o["status"] != "open", o["due"] or "9999", o["created"]))

    @api.get("/api/orders/customers", dependencies=guard)
    def customer_summary():
        from proofloop.monitor.orders import customers

        return customers(app.orders.all())

    @api.get("/api/business", dependencies=guard)
    def business():
        return app.settings.business.model_dump()

    @api.put("/api/business", dependencies=guard)
    def put_business(body: dict):
        """Who invoices are from. Nothing is filled in for you."""
        try:
            app.settings.business = Business.model_validate(body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        try:
            app.save_settings()
        except schema.NewerVersionError as e:
            raise HTTPException(409, str(e)) from e
        return app.settings.business.model_dump()

    @api.post("/api/orders", dependencies=guard)
    def add_order(body: dict):
        """Plan the run for an order, keep it as a queue (nothing starts), and follow it."""
        from proofloop.monitor.orders import due_ts, progress

        token = str(body.get("token", ""))
        sf = app.uploads.get(token)
        qty = body.get("quantity")
        try:
            if body.get("due"):
                due_ts(str(body["due"]))
        except ValueError as e:
            raise HTTPException(422, "give the due date as YYYY-MM-DD") from e
        settings = body.get("settings") or {}
        plan = plan_for(app, sf, qty, settings)
        try:
            schema.check_write("orders", app.orders.path)  # a newer orders.json: refuse before anything is queued
        except schema.NewerVersionError as e:
            raise HTTPException(409, str(e)) from e
        data, name = app.uploads.raw(token)
        price = body.get("price_each")
        try:
            price = None if price in (None, "") else float(price)
            defaults, st = plate_settings(app, {**settings, "loops": 1})
            prof = profile_or_404(app, defaults.profile_id)
            per_part = plate_cost(app, sf, prof, st, 1, {}, setup=False)["totals"]["per_part"]
            run = app.queue.add(name, data, settings, plan)
            try:
                order = app.orders.add(str(body.get("customer") or ""), name, int(qty or 0), body.get("due") or None,
                                       run, plan, price, per_part, str(body.get("notes") or ""))
            except Exception:  # never leave a run in the queue that no order follows
                app.queue.remove(run["id"])
                raise
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return progress(app, order)

    @api.patch("/api/orders/{oid}", dependencies=guard)
    def change_order(oid: str, body: dict):
        from proofloop.monitor.orders import due_ts, progress

        changes = {k: body[k] for k in ("customer", "due", "price_each", "notes", "status") if k in body}
        if changes.get("status") not in (None, "open", "done", "cancelled"):
            raise HTTPException(422, "status is open, done or cancelled")
        if "paid" in body and not isinstance(body["paid"], bool):
            raise HTTPException(422, "paid is true or false")
        try:
            if changes.get("due"):
                due_ts(changes["due"])
            if "paid" in body:
                changes.update(paid=body["paid"], paid_at=time.time() if body["paid"] else None)
            return progress(app, app.orders.update(oid, **changes))
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except schema.NewerVersionError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, "give the due date as YYYY-MM-DD") from e

    @api.delete("/api/orders/{oid}", dependencies=guard)
    def remove_order(oid: str, keep_run: bool = False):
        try:
            gone = app.orders.remove(oid)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except schema.NewerVersionError as e:
            raise HTTPException(409, str(e)) from e
        if not keep_run:
            try:
                app.queue.remove(gone["run"])
            except KeyError:
                pass
        return {"removed": oid}
