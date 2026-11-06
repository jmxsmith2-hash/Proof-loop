"""Print again: the loop files ProofLoop made and kept, sent again to a printer you pick.

The file is the one that was made (kept under ``files/``), not sliced again. It goes through the same
send path as a plate sent from Make a loop file: the same readiness checks, the same AMS tray matching
and the same confirmation of the printer name.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, HTTPException

from proofloop.app.routes.common import Guard, send_made
from proofloop.loopfiles import threemf
from proofloop.monitor.runout import run_of
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    def kept(key: str):
        job = app.store.job(key)
        if job is None:
            raise HTTPException(404, "no loop file called that")
        path = app.journal.file_for(key)
        if path is None:
            raise HTTPException(410, "file no longer kept")
        return job, path

    @api.get("/api/made", dependencies=guard)
    def history(limit: int = 30):
        """Loop files ProofLoop made, newest first: when, the printer last sent to, the loops, and whether the
        file is still kept (a file no longer kept can't be printed again)."""
        rows = []
        for j in app.store.jobs(limit=min(max(1, limit), 200)):
            sent = j.get("sent") or {}
            rows.append({
                "key": j["name"],
                "file": j.get("file") or j["name"],
                "made": j["ts"],
                "loops": j.get("loops"),
                "printer": sent.get("printer"),
                "sent": sent.get("ts"),
                "kept": app.journal.file_for(j["name"]) is not None,
            })
        return rows

    @api.post("/api/made/{key}/printers", dependencies=guard)
    def targets(key: str, body: dict | None = None):
        """Which printers this kept file could go to right now, as Make a loop file asks for a plate."""
        if not allow_send:
            raise HTTPException(403, "files can't be sent to printers from here")
        job, path = kept(key)
        sf = threemf.loads(path.read_bytes(), name=job.get("file") or key)
        return {"send_to_printer": app.settings.send_to_printer,
                "printers": app.send_readiness(sf, run=run_of(job)[1])}

    @api.post("/api/made/{key}/send", dependencies=guard)
    def reprint(key: str, body: dict):
        """Send the kept file to the printer named twice (``printer`` and ``confirm``), and start it."""
        if not allow_send:
            raise HTTPException(403, "files can't be sent to printers from here")
        name, confirm = str(body.get("printer") or ""), body.get("confirm")
        if not name or confirm != name:
            raise HTTPException(400, "confirm the printer to send it to")
        job, path = kept(key)
        r = SimpleNamespace(data=path.read_bytes(), filename=job.get("file") or key)
        return send_made(app, allow_send, name, confirm, r)
