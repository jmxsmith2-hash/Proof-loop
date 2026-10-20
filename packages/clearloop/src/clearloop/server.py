"""REST API and web dashboard."""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import __version__, threemf
from .cost import cost_per_part
from .ejector import CustomGcodeEjector
from .farm import Farm, _slug, process_options
from .gcode.processor import UnsupportedMaterial, process
from .orders import enqueue_order, shopify_lines, verify_shopify

log = logging.getLogger(__name__)
ROLES = {"viewer": 0, "operator": 1, "admin": 2}
STATIC = Path(__file__).parent / "static"


def create_app(farm: Farm) -> FastAPI:
    app = FastAPI(title="ClearLoop", version=__version__)
    tokens = {t.token: t.role for t in farm.cfg.farm.api_tokens}
    if not tokens:
        log.warning("no api_tokens configured: the API is open to anyone on this network")

    def role(required: str):
        def check(request: Request, authorization: str | None = Header(default=None)) -> str:
            if not tokens:
                return "admin"
            token = (authorization or "").removeprefix("Bearer ").strip() or request.query_params.get(
                "token", ""
            )
            have = tokens.get(token)
            if have is None or ROLES[have] < ROLES[required]:
                raise HTTPException(401 if have is None else 403, "missing or insufficient API token")
            return have

        return Depends(check)

    viewer, operator, admin = role("viewer"), role("operator"), role("admin")

    def ctl(name: str):
        c = farm.controllers.get(name)
        if c is None:
            raise HTTPException(404, f"no printer {name!r}")
        return c

    def pcfg(name: str):
        try:
            return farm.cfg.printer(name)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return (STATIC / "index.html").read_text()

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "printers": len(farm.controllers), "auth": bool(tokens)}

    # ---- printers ------------------------------------------------------------
    @app.get("/api/printers", dependencies=[viewer])
    def printers():
        return [c.status() for c in farm.controllers.values()]

    @app.get("/api/printers/{name}", dependencies=[viewer])
    def printer(name: str):
        return ctl(name).status()

    @app.post("/api/printers/{name}/cleared", dependencies=[operator])
    def cleared(name: str):
        ctl(name).mark_cleared()
        return ctl(name).status()

    @app.post("/api/printers/{name}/reverify", dependencies=[operator])
    def reverify(name: str):
        ctl(name).reverify()
        return ctl(name).status()

    @app.post("/api/printers/{name}/loop", dependencies=[operator])
    def set_loop(name: str, body: dict):
        ctl(name).set_enabled(bool(body.get("enabled", True)))
        return ctl(name).status()

    @app.post("/api/printers/{name}/stop", dependencies=[operator])
    def stop(name: str):
        c = ctl(name)
        c.link.stop()
        c.block("print stopped by operator", kind="stopped")
        return c.status()

    @app.post("/api/printers/{name}/plate", dependencies=[operator])
    def new_plate(name: str, body: dict):
        p = pcfg(name)
        plate = farm.store.replace_plate(
            name, body.get("type", p.plate.type), int(body.get("rated_cycles", p.plate.rated_cycles))
        )
        farm.store.add_event(name, "info", "plate_replaced", f"new {plate['type']} plate installed")
        return plate

    @app.post("/api/printers/{name}/magazine/reload", dependencies=[operator])
    def reload_magazine(name: str):
        ej = ctl(name).ejector
        if not isinstance(ej, CustomGcodeEjector):
            raise HTTPException(400, "this printer's ejector has no plate magazine")
        ej.reload()
        return ej.status()

    @app.get("/api/printers/{name}/snapshot", dependencies=[viewer])
    def snapshot(name: str):
        try:
            return Response(ctl(name).link.snapshot(), media_type="image/jpeg")
        except Exception as e:
            raise HTTPException(502, f"camera: {e}") from e

    @app.get("/api/printers/{name}/diff", dependencies=[viewer])
    def diff(name: str):
        path = farm.cfg.data_path / "diffs" / f"{name}-last-diff.png"
        if not path.exists():
            raise HTTPException(404, "no verification yet")
        return FileResponse(path, media_type="image/png")

    @app.post("/api/printers/{name}/reference", dependencies=[admin])
    def add_reference(name: str):
        verifier = farm.verifier_for(pcfg(name))
        if verifier is None:
            raise HTTPException(400, "verification is disabled for this printer")
        try:
            path = verifier.add_reference(ctl(name).link.snapshot())
        except Exception as e:
            raise HTTPException(502, f"camera: {e}") from e
        farm.store.add_event(name, "info", "reference_added", f"empty-plate reference {path.name} captured")
        return {"reference": path.name, "count": len(verifier.references)}

    @app.delete("/api/printers/{name}/references", dependencies=[admin])
    def clear_references(name: str):
        verifier = farm.verifier_for(pcfg(name))
        if verifier:
            verifier.clear_references()
        return {"count": 0}

    # ---- queue -----------------------------------------------------------------
    @app.get("/api/queue", dependencies=[viewer])
    def queue(status: str | None = None):
        return farm.store.jobs(status)

    @app.post("/api/queue", dependencies=[operator])
    async def enqueue(
        file: UploadFile = File(...),
        copies: int = Form(1),
        printer: str | None = Form(None),
        models: str = Form(""),
        priority: int = Form(0),
    ):
        name = Path(file.filename or "upload.gcode.3mf").name
        dest = (
            farm.library_dir()
            / f"{_slug(name.split('.')[0])}-{secrets.token_hex(3)}{''.join(Path(name).suffixes)}"
        )
        dest.write_bytes(await file.read())
        try:
            ids = farm.enqueue_file(
                dest,
                name=name,
                copies=copies,
                printer=printer or None,
                models=[m.strip().lower() for m in models.split(",") if m.strip()],
                priority=priority,
            )
        except ValueError as e:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, str(e)) from e
        return {"jobs": ids}

    @app.delete("/api/queue/{job_id}", dependencies=[operator])
    def cancel(job_id: int):
        if not farm.store.cancel_job(job_id):
            raise HTTPException(409, "only queued jobs can be cancelled")
        return {"cancelled": job_id}

    # ---- history ---------------------------------------------------------------
    @app.get("/api/events", dependencies=[viewer])
    def events(limit: int = 100, printer: str | None = None):
        return farm.store.events(limit, printer)

    @app.get("/api/loops", dependencies=[viewer])
    def loops(limit: int = 100, printer: str | None = None):
        return farm.store.loops(printer, limit)

    @app.get("/api/stats", dependencies=[viewer])
    def stats(days: float = 30):
        return farm.stats(days)

    # ---- processing API (free, local) --------------------------------------------
    @app.post("/api/process", dependencies=[operator])
    async def process_file(
        file: UploadFile = File(...),
        printer: str = Form(...),
        copies: int = Form(1),
        plate: int | None = Form(None),
    ):
        p = pcfg(printer)
        raw = await file.read()
        try:
            sliced = threemf.loads(raw, plate=plate)
            result = process(
                sliced,
                process_options(
                    farm.cfg,
                    p,
                    farm.controllers[p.name].ejector if p.name in farm.controllers else None,
                    copies,
                ),
            )
        except (UnsupportedMaterial, ValueError) as e:
            raise HTTPException(422, str(e)) from e
        name = Path(file.filename or "plate.gcode.3mf").name
        cost = cost_per_part(
            sliced.info.filament_grams * copies,
            sliced.info.print_seconds * copies,
            result.cycle_seconds * copies,
            len(result.objects) * copies,
            farm.cfg.costs,
            p.plate.rated_cycles,
        )
        headers = {
            "Content-Disposition": f'attachment; filename="clearloop_{name}"',
            "X-ClearLoop-Printer": p.profile().model,
            "X-ClearLoop-Eject-Mode": result.manifest["eject_mode"],
            "X-ClearLoop-Warnings": json.dumps(result.warnings)[:4000],
            "X-ClearLoop-Cycle-Seconds": str(round(result.cycle_seconds)),
            "X-ClearLoop-Cost-Per-Part": json.dumps(cost),
        }
        return Response(result.data, media_type="application/octet-stream", headers=headers)

    # ---- orders -----------------------------------------------------------------
    @app.post("/api/orders/shopify")
    async def shopify(request: Request, x_shopify_hmac_sha256: str | None = Header(default=None)):
        raw = await request.body()
        if not verify_shopify(raw, x_shopify_hmac_sha256, farm.cfg.farm.shopify_webhook_secret):
            raise HTTPException(401, "bad Shopify signature")
        order = json.loads(raw)
        ref = f"shopify:{order.get('name') or order.get('id')}"
        return enqueue_order(farm, shopify_lines(order), ref, _config_dir(farm))

    @app.post("/api/orders", dependencies=[operator])
    def generic_order(body: dict):
        lines = [(str(i["sku"]), int(i["quantity"])) for i in body.get("lines", [])]
        if not lines:
            raise HTTPException(400, "body needs lines: [{sku, quantity}]")
        return enqueue_order(farm, lines, str(body.get("order_ref", "manual")), _config_dir(farm))

    @app.exception_handler(KeyError)
    async def key_error(request: Request, exc: KeyError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    return app


def _config_dir(farm: Farm) -> Path | None:
    return getattr(farm, "config_dir", None)
