"""The cameras: the live-ish view on a printer's card, the camera settings, the timelapses and the proof photos."""

from __future__ import annotations

import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse

from proofloop.app.routes.common import Guard
from proofloop.core.settings import CameraSettings
from proofloop.monitor.camera_live import LiveCamera, polled
from proofloop.monitor.service import App
from proofloop.monitor.timelapse import ID, folder_name
from proofloop.monitor.watch import CAMERA_ERRORS

FRAME_AGE = 1.5  # seconds a picture is shared between everyone looking, so the camera is asked once for them all


class Frames:
    """The latest picture of each printer's camera. Several viewers (and tabs) share one request to the camera."""

    def __init__(self, age: float = FRAME_AGE):
        self.age = age
        self._frames: dict[str, tuple[float, bytes]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, name: str, fetch) -> bytes:
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        with lock:  # whoever comes second waits for the first one's picture
            at, jpeg = self._frames.get(name, (0.0, b""))
            if jpeg and time.monotonic() - at < self.age:
                return jpeg
            jpeg = fetch()
            self._frames[name] = (time.monotonic(), jpeg)
            return jpeg

    def forget(self, name: str) -> None:
        with self._guard:
            self._frames.pop(name, None)


def register(api: FastAPI, app: App, guard: Guard, allow_send: bool = True) -> None:
    frames = Frames()

    def watch(name: str):
        w = app.watches.get(name)
        if w is None:
            raise HTTPException(404, f"no printer {name!r}")
        return w

    @api.get("/api/monitor/{name}/camera", dependencies=guard)
    def camera(name: str):
        """The latest picture, at most a second or two old. The page asks again every second or two while
        the camera is open (no stream: nothing to hold open or to stop)."""
        w = watch(name)
        try:
            jpeg = frames.get(name, w.link.snapshot)
        except CAMERA_ERRORS as e:
            frames.forget(name)
            raise HTTPException(502, f"camera: {e}") from e
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    polls: dict[str, LiveCamera] = {}  # printers whose connection has no live camera of its own

    def live_camera(name: str) -> LiveCamera:
        w = watch(name)
        own = getattr(w.link, "live", None)
        if isinstance(own, LiveCamera):
            return own
        snap = w.link.snapshot
        return polls.setdefault(name, LiveCamera(lambda: polled(snap)))

    @api.get("/api/monitor/{name}/live", dependencies=guard)
    async def live(name: str, request: Request, frames: int = 0):
        """The camera as video: JPEG after JPEG (``multipart/x-mixed-replace``) for as long as the page reads.

        A part that is ``text/plain`` says what's wrong while the camera can't be reached; the stream
        stays open and goes on when it's back. ``frames`` stops after that many pictures (0: never).
        """
        cam = live_camera(name)

        async def parts():
            seq, sent = 0, 0
            while not await request.is_disconnected():
                new, jpeg, error = await run_in_threadpool(cam.next_frame, seq, 4.0)
                if new == seq:
                    if error:
                        msg = error.encode()
                        yield b"--frame\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n%s\r\n" % (len(msg), msg)
                    continue
                seq, sent = new, sent + 1
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n" % (len(jpeg), jpeg)
                if frames and sent >= frames:
                    return

        return StreamingResponse(parts(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @api.get("/api/camera-settings", dependencies=guard)
    def get_camera_settings():
        return app.settings.camera.model_dump()

    @api.put("/api/camera-settings", dependencies=guard)
    def put_camera_settings(body: dict):
        if not allow_send:
            raise HTTPException(403, "this can only be changed in the ProofLoop window")
        try:
            app.settings.camera = CameraSettings.model_validate({**app.settings.camera.model_dump(), **body})
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        app.save_settings()
        return app.settings.camera.model_dump()

    @api.get("/api/monitor/{name}/timelapses", dependencies=guard)
    def timelapses(name: str):
        return watch(name).timelapse.listing()

    @api.get("/api/monitor/{name}/timelapses/{id}", dependencies=guard)
    def timelapse(name: str, id: str, download: bool = False):
        found = watch(name).timelapse.file(id) if ID.match(id) else None
        if found is None:
            raise HTTPException(404, "no such timelapse")
        path, media_type = found
        headers = {"Content-Disposition": f'attachment; filename="{folder_name(name)}-{id}{path.suffix}"'} if download else None
        return FileResponse(path, media_type=media_type, headers=headers)

    @api.get("/api/proofs/{pid}", dependencies=guard)
    def proof_photo(pid: str):
        """One proof photo: a loop's finished plate, before the sweep."""
        path = app.proofs.file(pid)
        if path is None:
            raise HTTPException(404, "no such picture")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
