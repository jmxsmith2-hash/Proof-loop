"""ProofLoop running in the background: the window's server, the printer monitor and phone access.

The window is only a view: closing it leaves the service running, so the printers stay watched.
The service tells you (through ClearLoop's notifications) when it stops, and when it starts again
after an unexpected stop such as a crash, a power cut or Windows restarting for an update, with how
long the printers went unwatched.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import uvicorn

from .config import DEFAULT_PORT, Settings, home
from .shell import Shell

log = logging.getLogger(__name__)
BEAT_S = 60


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 2:
        return "about a minute"
    if m < 90:
        return f"about {m} minutes"
    return f"about {m // 60} h {m % 60} min"


class Service:
    def __init__(self, port: int = DEFAULT_PORT, clearloop_data=None, synthux_config=None, connect: bool = True,
                 link_factory=None):
        self.port = port
        self.settings = Settings.load()
        self.shell = Shell(port, clearloop_data=clearloop_data, synthux_config=synthux_config, connect=connect,
                           service=self, link_factory=link_factory)
        self.phone = None
        if self.shell.clearloop:
            from .phone import Phone

            self.phone = Phone(self.shell.clearloop, self.settings)
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.stopped = threading.Event()  # stopping has begun
        self.done = threading.Event()  # and finished: only then may the process end
        self.on_stop: list = []  # e.g. the tray icon
        self.on_beat: list = []
        self._restarts = 0
        self.marker = home() / "running.json"

    # ---- what the tray and window show ---------------------------------------------
    def printers(self) -> list:
        return list(self.shell.clearloop.watches.values()) if self.shell.clearloop else []

    def summary(self) -> str:
        ws = self.printers()
        if not self.shell.clearloop:
            return "ProofLoop (synthux only)"
        if not ws:
            return "ProofLoop: no printers added yet"
        busy = sum(1 for w in ws if w.link.state.gcode_state in ("RUNNING", "PREPARE", "PAUSE"))
        return f"ProofLoop: watching {len(ws)} printer{'s' if len(ws) != 1 else ''}" + \
            (f", {busy} printing" if busy else "")

    def notify(self, level: str, message: str, wait: bool = False) -> None:
        """Through ClearLoop: its event log, and your ntfy / Discord / webhook targets."""
        cl = self.shell.clearloop
        if not cl:
            return
        try:
            cl.notifier.send(cl.store.add_event(None, level, "proofloop", message), None, wait=wait)
        except Exception:  # noqa: BLE001 - a failed notice must never stop the service
            log.exception("could not send a notice")

    # ---- run ------------------------------------------------------------------------
    def _serve(self) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(self.shell.api, host="127.0.0.1", port=self.port, log_level="warning",
                           timeout_graceful_shutdown=3)
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="proofloop-window")
        self.thread.start()
        end = time.time() + 30
        while not self.server.started and self.thread.is_alive() and time.time() < end:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError(f"couldn't listen on port {self.port}")

    def _beat(self) -> None:
        tmp = self.marker.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "beat": time.time(), "port": self.port}), encoding="utf-8")
        tmp.replace(self.marker)

    def start(self) -> None:
        unclean = None
        try:
            unclean = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self._serve()
        if self.phone and self.settings.phone:
            try:
                self.phone.start()
            except RuntimeError as e:
                self.notify("warning", f"phone access is off: {e}")
        self._beat()
        threading.Thread(target=self._heartbeat, daemon=True, name="proofloop-beat").start()
        n = len(self.printers())
        if unclean and unclean.get("beat"):
            since = time.strftime("%H:%M", time.localtime(unclean["beat"]))
            self.notify(
                "warning",
                f"ProofLoop started again after an unexpected stop (a crash, power cut or restart). Your "
                f"printers weren't watched for {_ago(time.time() - unclean['beat'])}, from {since}.",
            )
        elif n:
            self.notify("info", f"ProofLoop is running and watching {n} printer{'s' if n != 1 else ''}")
        log.info("ProofLoop service started on port %s", self.port)

    def _heartbeat(self) -> None:
        while not self.stopped.wait(BEAT_S):
            try:
                self._beat()
            except OSError:
                log.exception("heartbeat")
            for fn in list(self.on_beat):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    log.exception("beat callback")
            if self.thread and not self.thread.is_alive() and not self.stopped.is_set():
                # The window's server died on its own: bring it back (the monitor kept running).
                self._restarts += 1
                log.error("the window's server stopped; restarting it (%s)", self._restarts)
                if self._restarts <= 5:
                    try:
                        self._serve()
                    except RuntimeError:
                        log.exception("restart failed")

    def stop(self, reason: str = "you quit ProofLoop") -> None:
        if self.stopped.is_set():
            return
        self.stopped.set()
        ws = self.printers()
        if ws:
            busy = [w.name for w in ws if w.link.state.gcode_state in ("RUNNING", "PREPARE", "PAUSE")]
            self.notify(
                "warning",
                f"ProofLoop stopped ({reason}): your printers are not being watched"
                + (f". Still printing: {', '.join(busy)}" if busy else ""),
                wait=True,  # before this process ends
            )
        if self.phone:
            self.phone.stop()
        if self.server:
            self.server.should_exit = True
            if self.thread:
                self.thread.join(timeout=10)
        self.shell.close()
        try:
            self.marker.unlink()
        except OSError:
            pass
        for fn in list(self.on_stop):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("stop callback")
        log.info("ProofLoop service stopped: %s", reason)
        self.done.set()

    def stop_soon(self, reason: str) -> None:
        """Stop from a request handler, after it has answered."""
        threading.Timer(0.3, self.stop, args=(reason,)).start()

    def wait(self) -> None:
        """Until ProofLoop has stopped completely (a quit from the window stops it on another thread)."""
        while not self.done.wait(1):
            pass
