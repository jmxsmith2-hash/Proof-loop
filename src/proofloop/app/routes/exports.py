"""Spreadsheet exports (CSV) and invoices: for your books, your accountant and your customers."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from proofloop.app.routes.common import Guard
from proofloop.core import schema, theme
from proofloop.monitor import exports
from proofloop.monitor.service import App


def csv_response(columns: list[str], rows: list[dict], name: str) -> Response:
    return Response(
        exports.to_csv(columns, rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    def span(start: str | None, end: str | None) -> tuple[float, float]:
        try:
            return exports.date_range(start, end)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @api.get("/api/orders/export.csv", dependencies=guard)
    def orders_csv(from_: str | None = Query(None, alias="from"), to: str | None = None):
        """Orders taken in the range, with what's made, when it will be ready and what it earns."""
        return csv_response(exports.ORDER_COLUMNS, exports.orders_rows(app, *span(from_, to)), "proofloop-orders.csv")

    @api.get("/api/outcomes/export.csv", dependencies=guard)
    def history_csv(from_: str | None = Query(None, alias="from"), to: str | None = None):
        """How every print ended: printer, file, when, result, layer and why."""
        return csv_response(exports.HISTORY_COLUMNS, exports.history_rows(app, *span(from_, to)),
                            "proofloop-history.csv")

    @api.get("/api/costs/export.csv", dependencies=guard)
    def costs_csv(from_: str | None = Query(None, alias="from"), to: str | None = None, by: str = "order"):
        """Production and costs per order (default), per day or per month."""
        if by not in exports.BY:
            raise HTTPException(422, "by is order, day or month")
        return csv_response(exports.cost_columns(by), exports.costs_rows(app, *span(from_, to), by=by),
                            "proofloop-costs.csv")

    @api.get("/api/orders/{oid}/invoice", dependencies=guard, response_class=HTMLResponse)
    def invoice(oid: str):
        """The order's invoice as a page to print or save as PDF. The first time gives it its number."""
        order = app.orders.get(oid)
        biz = app.settings.business
        try:
            inv, new = app.orders.invoice(oid, biz.prefix, biz.next_number)
        except schema.NewerVersionError as e:
            raise HTTPException(409, str(e)) from e
        if new:
            biz.next_number = inv["number"] + 1
            try:
                app.save_settings()
            except schema.NewerVersionError:
                pass  # the number is still never used twice: the next one counts up from the orders
        order = {**order, "invoice": inv}
        return theme.environment("proofloop.app", "templates").get_template("invoice.html.j2").render(
            **exports.invoice_data(app, order))
