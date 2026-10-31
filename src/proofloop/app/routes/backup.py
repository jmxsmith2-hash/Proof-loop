"""Backup and restore."""

from __future__ import annotations

import time

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response

from proofloop.app import backup as backups
from proofloop.app.routes.common import Guard
from proofloop.monitor.service import App


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    """``allow_send=False`` for phone access: a phone can neither download a backup (it holds the printers'
    access codes and every password) nor restore one."""
    @api.get("/api/backup", dependencies=guard)
    def backup():
        if not allow_send:
            raise HTTPException(403, "a backup can only be made in the ProofLoop window: it holds your "
                                     "printers' access codes")
        stamp = time.strftime("%Y%m%d-%H%M")
        return Response(
            backups.make(app),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="proofloop-backup-{stamp}.zip"'},
        )

    @api.post("/api/restore", dependencies=guard)
    async def restore(file: UploadFile = File(...)):
        if not allow_send:
            raise HTTPException(403, "a backup can only be restored in the ProofLoop window")
        raw = await file.read(backups.MAX_RESTORE + 1)
        try:
            return backups.restore(app, raw)
        except ValueError as e:
            raise HTTPException(422, f"can't restore: {e}") from e
