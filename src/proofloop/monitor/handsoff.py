"""Hands-off queue: a printer you switch to hands-off gets its next queued job by itself.

When the printer is free, its last job from the queue finished (not failed), and a plate check since
then says the plate is empty, ProofLoop sends the printer's next waiting job. With no fresh check,
ProofLoop asks the camera for one (at most every few minutes); a plate with parts on it stops
everything until you clear it, as it does between loops.

Your *ask before each send* setting still applies: while it's on, ProofLoop first tells you which job
it's about to send and where, and sends it a couple of minutes later unless you press Hold. Sending
to printers has to be on, and quiet hours hold it like a loop. A send that fails holds the job and
tells you; nothing is retried by itself.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from proofloop.loopfiles import threemf
from proofloop.loopfiles.lint import lint
from proofloop.monitor.send import BUSY

if TYPE_CHECKING:
    from proofloop.monitor.service import App

log = logging.getLogger(__name__)

HOLD_S = 120.0  # notice before a send while "ask before each send" is on
FRESH_S = 1800.0  # a plate check older than this is taken again
ASK_EVERY_S = 300.0  # at most one plate check asked for per printer in this long


class HandsOff:
    def __init__(self, app: App):
        self.app = app
        # Set by the window's API (app/routes/queue.py): makes a queued job's loop file, as Send does.
        self.make: Callable[[str, str], Any] | None = None
        self._asked: dict[str, float] = {}

    def on(self, printer: str) -> bool:
        cfg = next((p for p in self.app.settings.printers if p.name == printer), None)
        return bool(cfg and cfg.hands_off and self.app.settings.send_to_printer)

    def tick(self, now: float | None = None) -> list[str]:
        """Send what is due. Returns what happened, one line per printer acted on (for tests and the log)."""
        if self.make is None or not self.app.settings.send_to_printer:
            return []
        now = time.time() if now is None else now
        done = []
        for run, job in self.app.queue.next_jobs(self.app):
            printer = job["printer"]
            if not self.on(printer) or job.get("held"):
                continue
            w = self.app.watches.get(printer)
            if w is None:
                continue
            s = w.link.state
            if not s.online or s.gcode_state.upper() in BUSY or s.gcode_state.upper() == "FAILED":
                continue
            if w.waiting_for_you or w.quiet_until() or getattr(w, "_checking", False):
                continue
            since = max(float(run.get("created") or 0), w.ended_at)
            check = w.last_check
            if check is None or w.last_check_ts < since or now - w.last_check_ts > FRESH_S:
                if now - self._asked.get(printer, 0.0) >= ASK_EVERY_S:  # look at the plate first
                    self._asked[printer] = now
                    w.check_again()
                    done.append(f"{printer}: checking the plate")
                continue
            if not check.clear:
                continue  # parts on the plate: the watch has told you; it waits for you to clear it
            if self.app.settings.send_confirm:
                if not job.get("auto_at"):
                    self.app.queue.mark(run["id"], job["id"], auto_at=now + HOLD_S)
                    self._event(printer, "warning", "queue_auto_soon",
                                f"Hands-off: sending {run['plate']} ({self._loops(job)}) to {printer} in "
                                f"{int(HOLD_S // 60)} minutes. Press Hold under Printers › Overview to stop it.")
                    done.append(f"{printer}: sending soon")
                    continue
                if now < float(job["auto_at"]):
                    continue
            done.append(self.send(run, job))
        return done

    def send(self, run: dict, job: dict) -> str:
        printer = job["printer"]
        assert self.make is not None
        try:
            r = self.make(run["id"], job["id"])
            made = threemf.loads(r.data, name=r.filename)
            danger = [f.title for f in lint(made) if f.level == "danger"]
            self.app.send(printer, r.filename, r.data, made, danger)
        except Exception as e:  # broad: whatever stops the send holds the job and is told, never retried
            log.warning("hands-off send to %s failed", printer, exc_info=True)
            self.app.queue.mark(run["id"], job["id"], held=True, auto_at=None)
            self._event(printer, "error", "queue_auto_failed",
                        f"Hands-off couldn't send {run['plate']} to {printer}: {e}. It's held: send it "
                        "yourself under Printers › Overview.")
            return f"{printer}: failed"
        self.app.queue.mark(run["id"], job["id"], status="sent", file=r.filename, at=time.time(), why=None,
                            loop=None, auto_at=None)
        self._event(printer, "info", "queue_auto_sent",
                    f"Hands-off: the plate was empty, so {printer} started {run['plate']} ({self._loops(job)}).")
        return f"{printer}: sent"

    @staticmethod
    def _loops(job: dict) -> str:
        return f"{job['loops']} loop{'s' if job['loops'] != 1 else ''}"

    def _event(self, printer: str, level: str, kind: str, message: str) -> None:
        self.app._emit(self.app.store.add_event(printer, level, kind, message))
