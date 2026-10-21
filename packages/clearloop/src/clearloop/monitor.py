"""ClearLoop Monitor: closes the loop between loops, fail-safe.

When "Wait for ClearLoop Monitor" is on, every loop except the last ends with
``M400 U1`` (Bambu's pause command), so the printer holds after the sweep. The
monitor sees that pause, takes a camera picture, compares it with your pictures
of the empty plate and only then sends ``resume``. If parts are left behind,
or the camera or monitor isn't available, the printer just keeps waiting, with
heaters off, until you clear the plate and press Resume. A crash or a missed
event can never start the next print on a dirty plate.

A pause counts as a loop-end pause only when the running file is a loop file
(``FL_S1_``/``FL_S2_``) and the printer reports the last layer as finished.
Any other pause (filament runout, a user) is reported and never resumed.

With filament tracking on, each finished loop is charged to its spools, a
loop that stops part-way is charged what its finished layers used, and a
loop-end pause stays waiting when a spool can't cover the next loop.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .printer.ams import valid_tray
from .printer.state import PrinterState
from .store import Store
from .vision import BedCheck, BedVerifier

log = logging.getLogger(__name__)

PHASE = {
    1: "starting",
    2: "cooling, door open",
    3: "bending the plate",
    4: "safety check",
    5: "door closing",
}

SENTINEL_TEXT = {
    1: "FarmBoard reset: print starting, door closing",
    2: "print done: door opening, cooling",
    3: "bending the plate",
    4: "FarmBoard safety check",
    5: "door closing",
}


def job_names(s: PrinterState) -> list[str]:
    return [n.rsplit("/", 1)[-1] for n in (s.subtask_name, s.gcode_file) if n]


def is_loop_job(s: PrinterState) -> bool:
    return any(is_loop_file(n) for n in job_names(s))


def is_loop_file(name: str) -> bool:
    # Calibration prints pause on purpose so you can watch; the monitor must never resume those.
    return name.startswith(("FL_S1_", "FL_S2_")) and "_CALIBRATION-" not in name


class PrinterWatch:
    def __init__(
        self,
        name: str,
        link,
        verifier: BedVerifier,
        store: Store,
        emit: Callable[..., None] | None = None,
        auto_resume: bool = True,
        quiet_until: Callable[[], str | None] | None = None,
        settle_s: float = 3.0,
        run_async: Callable[[Callable[[], None]], None] | None = None,
        attempts: int = 3,
        retry_s: float = 2.0,
        filament=None,
    ):
        self.name = name
        self.link = link
        self.verifier = verifier
        self.store = store
        self._emit = emit or (lambda e, image=None: None)
        self.auto_resume = auto_resume
        # Returns "HH:MM" while quiet hours are on (no new loop starts), else None.
        self.quiet_until = quiet_until or (lambda: None)
        self.pending_resume = False  # plate verified empty during quiet hours; resume when they end
        self.settle_s = settle_s
        self.run_async = run_async or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.attempts = attempts
        self.retry_s = retry_s
        self.lock = threading.RLock()
        self.status = "waiting for printer"
        self.waiting_for_you = False
        self.last_check: BedCheck | None = None
        self.last_check_ts = 0.0
        self._online = False
        self._gcode_state = ""
        self._sentinel = 0.0
        self._seen_hms: set[str] = set()
        self._checking = False
        self.phase = ""
        self.loop_no = 0
        self._job = ""
        self._marks: dict[int, float] = {}  # sentinel -> when it arrived, for the current loop
        self._last_layer = 0
        self._last_start = 0.0
        # Filament: an object with loop_done / loop_failed / blocker / on_ams, or None.
        self.filament = filament
        self._printed = False  # the current loop's print has finished (and been charged)
        self._trays_loop: list[int] = []  # AMS trays the printer fed from during this loop
        self._max_layer = 0

    def event(
        self, level: str, kind: str, message: str, data: dict | None = None, image: bytes | None = None
    ) -> None:
        self._emit(self.store.add_event(self.name, level, kind, message, data), image=image)

    # ---- printer updates -----------------------------------------------------------
    def on_state(self, s: PrinterState) -> None:
        with self.lock:
            if s.online != self._online:
                self._online = s.online
                self.event(
                    "info" if s.online else "warning",
                    "connection",
                    "connected" if s.online else "lost connection to the printer",
                )
            for h in s.hms:
                if h.full not in self._seen_hms:
                    self._seen_hms.add(h.full)
                    level = "error" if h.level in ("error", "warning") else "info"
                    self.event(level, "hms", f"printer reports HMS {h.code_str} ({h.level})")
            self._seen_hms &= {h.full for h in s.hms}
            if s.commands_rejected:
                self.status = "the printer rejects commands: turn on Developer Mode (LAN-only mode)"

            self._track_loop(s)
            self._call("on_ams", s)

            if s.gcode_state != self._gcode_state:
                prev, self._gcode_state = self._gcode_state, s.gcode_state
                self._on_gcode_state(prev, s)

    def _track_loop(self, s: PrinterState) -> None:
        """Loop number, current phase and measured phase times, from what the printer reports."""
        now = time.time()
        job = "|".join(job_names(s))
        if job and job != self._job:
            self._job, self.loop_no, self._marks, self._last_layer = job, 1, {}, 0
            self._new_loop()
        loop_job = is_loop_job(s)
        if loop_job and s.gcode_state == "RUNNING":
            if valid_tray(s.ams.tray_now) and s.ams.tray_now not in self._trays_loop:
                self._trays_loop.append(s.ams.tray_now)
            self._max_layer = max(self._max_layer, s.layer_num)
        t = s.nozzle_target
        if t != self._sentinel:
            self._sentinel = t
            code = int(t) if t in PHASE else 0
            if code and loop_job:
                self.phase = PHASE[code]
                self.event("info", "loop", SENTINEL_TEXT[code])
                if code == 1:
                    if 1 in self._marks:
                        self._loop_printed(s)  # never charged twice; covers a missed T1
                        self.loop_no += 1
                        self.store.add_timing(self.name, "loop", now - self._marks[1])
                    self._marks = {1: now}
                    self._last_start = now
                    self._new_loop()
                else:
                    self._marks[code] = now
                if code == 2:
                    self._loop_printed(s)
                if code == 3:
                    self.store.bump(self.name, "bends")
                    if 2 in self._marks:
                        self.store.add_timing(self.name, "cooldown", now - self._marks[2])
                if code == 5 and 2 in self._marks:
                    self.store.add_timing(self.name, "cycle", now - self._marks[2])
        # Stage 1 files have no FarmBoard signals: a new loop shows as the layer count starting over.
        if (
            loop_job
            and s.total_layers
            and self._last_layer >= s.total_layers
            and 0 < s.layer_num < self._last_layer
        ):
            if now - self._last_start > 600:
                self._loop_printed(s)
                self.loop_no += 1
                self._last_start = now
                self._new_loop()
        if loop_job and s.layer_num and s.layer_num != self._last_layer and s.gcode_state == "RUNNING":
            if not self._marks or max(self._marks) == 1:
                self.phase = (
                    f"printing layer {s.layer_num}/{s.total_layers}" if s.total_layers else "printing"
                )
        self._last_layer = s.layer_num

    def _on_gcode_state(self, prev: str, s: PrinterState) -> None:
        st = s.gcode_state
        if st in ("RUNNING", "PREPARE"):
            self.waiting_for_you = False
            self.status = f"printing {s.subtask_name}" if s.subtask_name else "printing"
        elif st == "PAUSE":
            if self.loop_end_pause(s):
                self._loop_printed(s)
                self.status = "loop finished: checking the plate"
                self._start_check("loop end")
            elif prev:
                self.status = "paused"
                self.event(
                    "warning",
                    "paused",
                    "the printer paused (not a loop-end pause); ClearLoop won't resume it",
                )
        elif st == "FINISH" and prev:
            if is_loop_job(s):
                self._loop_printed(s)
            self.status = "finished: checking the plate"
            self._start_check("finished")
        elif st == "FAILED" and prev:
            self.status = "print failed or cancelled"
            self.event("error", "failed", "the print failed or was cancelled; check the plate")
            if is_loop_job(s) and not self._printed:
                self._printed = True
                # layer_num is the layer being printed; the ones before it are finished.
                self._call("loop_failed", s, max(self._max_layer, s.layer_num) - 1)
        elif st == "IDLE":
            self.status = "idle"

    # ---- filament ---------------------------------------------------------------------
    def _new_loop(self) -> None:
        self._printed, self._trays_loop, self._max_layer = False, [], 0

    def _loop_printed(self, s: PrinterState) -> None:
        if not self._printed and is_loop_job(s):
            self._printed = True
            self._call("loop_done", s)

    def _call(self, what: str, *args):
        fn = getattr(self.filament, what, None) if self.filament else None
        if fn is None:
            return None
        try:
            return fn(self, *args)
        except Exception:  # filament bookkeeping must never stop the plate check
            log.exception("%s: filament %s failed", self.name, what)
            return None

    @property
    def trays_this_loop(self) -> list[int]:
        return list(self._trays_loop)

    def loop_end_pause(self, s: PrinterState) -> bool:
        return is_loop_job(s) and s.total_layers > 0 and s.layer_num >= s.total_layers

    # ---- plate checks ---------------------------------------------------------------
    def _start_check(self, reason: str, settle: float | None = None) -> None:
        if self._checking:
            return
        self._checking = True
        wait = self.settle_s if settle is None else settle
        self.run_async(lambda: self._check(reason, wait))

    def _check(self, reason: str, settle: float) -> None:
        try:
            if settle:
                time.sleep(settle)
            if not self.verifier.references:
                self._finish_check(
                    reason,
                    None,
                    "no picture of the empty plate yet: add one in Monitor, then press 'Check again'",
                )
                return
            check, err, jpeg = None, None, None
            for attempt in range(self.attempts):
                try:
                    jpeg = self.link.snapshot()
                    check = self.verifier.check(jpeg)
                    break
                except Exception as e:  # cameras drop frames; retry before giving up
                    err = e
                    if attempt + 1 < self.attempts:
                        time.sleep(self.retry_s)
            self._finish_check(reason, check, None if check else f"camera: {err}", jpeg)
        finally:
            with self.lock:
                self._checking = False

    def _finish_check(
        self, reason: str, check: BedCheck | None, problem: str | None, jpeg: bytes | None = None
    ) -> None:
        with self.lock:
            self.last_check = check
            self.last_check_ts = time.time()
            if check is not None and check.diff_png:
                try:
                    self.verifier.dir.mkdir(parents=True, exist_ok=True)
                    (self.verifier.dir / "last-diff.png").write_bytes(check.diff_png)
                except OSError:
                    log.warning("%s: could not save the difference image", self.name)
            resumed = False
            if check is None:
                self.waiting_for_you = reason == "loop end"
                self.status = "waiting for you: " + (problem or "plate not checked")
                self.event(
                    "error",
                    "check_failed",
                    f"plate not checked ({reason}): {problem}. The printer keeps waiting.",
                )
            elif check.clear:
                quiet = self.quiet_until()
                at_pause = reason == "loop end" and self.link.state.gcode_state == "PAUSE"
                short = self._call("blocker", self.link.state) if at_pause and self.auto_resume else None
                if short:
                    self.pending_resume = False
                    self.waiting_for_you = True
                    self.status = f"plate empty, but {short}. Swap or top up the spool, then press Resume"
                    self.event(
                        "error",
                        "filament_short",
                        f"plate empty, but {short}. The printer keeps waiting.",
                        check.as_dict(),
                    )
                    self.store.add_check(self.name, reason, True, check.changed_fraction, False)
                    return
                if at_pause and self.auto_resume and quiet:
                    self.pending_resume = True
                    self.status = f"plate empty: the next loop starts when quiet hours end ({quiet})"
                    self.event(
                        "info",
                        "quiet_hours",
                        f"plate empty; holding the next loop until {quiet} (quiet hours)",
                    )
                    self.store.add_check(self.name, reason, True, check.changed_fraction, False)
                    return
                if at_pause and self.auto_resume:
                    resumed = bool(self.link.resume())
                    self.pending_resume = False
                self.waiting_for_you = reason == "loop end" and not resumed
                self.status = "plate empty: next loop started" if resumed else "plate empty"
                self.event(
                    "info",
                    "plate_clear",
                    f"plate empty ({reason})" + ("; resumed the next loop" if resumed else ""),
                    check.as_dict(),
                )
            else:
                self.waiting_for_you = reason == "loop end"
                self.status = (
                    "parts left on the plate: clear it, then press 'Check again' or Resume on the printer"
                )
                self.event(
                    "error",
                    "plate_not_clear",
                    f"parts left on the plate ({reason}). The printer is waiting; clear the plate.",
                    check.as_dict(),
                    image=jpeg,
                )
            self.store.add_check(
                self.name,
                reason,
                bool(check and check.clear),
                check.changed_fraction if check else None,
                resumed,
            )

    # ---- actions from the app ---------------------------------------------------------
    def check_again(self) -> None:
        reason = (
            "loop end"
            if self.loop_end_pause(self.link.state) and self.link.state.gcode_state == "PAUSE"
            else "manual"
        )
        self._start_check(reason, settle=0.0)

    def tick(self) -> None:
        """Called every few seconds: start the held loop once quiet hours are over (after a fresh check)."""
        with self.lock:
            if self.pending_resume and not self.quiet_until() and self.link.state.gcode_state == "PAUSE":
                self.pending_resume = False
                self.check_again()

    def reset_bends(self) -> None:
        self.store.reset_counter(self.name, "bends")
        self.event("info", "plate", "bend counter reset (new build plate)")

    def resume_anyway(self) -> bool:
        with self.lock:
            ok = bool(self.link.resume())
            self.event("warning", "manual_resume", "resumed by you without a passing plate check")
            self.waiting_for_you = False
            return ok

    def as_dict(self) -> dict:
        with self.lock:
            return {
                "name": self.name,
                "status": self.status,
                "waiting_for_you": self.waiting_for_you,
                "references": len(self.verifier.references),
                "last_check": self.last_check.as_dict() if self.last_check else None,
                "last_check_ts": self.last_check_ts,
                "printer": self.link.state.as_dict(),
                "stats": self.store.stats(self.name, time.time() - 7 * 86400),
                "phase": self.phase,
                "loop_no": self.loop_no,
                "pending_resume": self.pending_resume,
                "timings": self.store.timings(self.name),
                "bends": self.store.counter(self.name, "bends"),
            }
