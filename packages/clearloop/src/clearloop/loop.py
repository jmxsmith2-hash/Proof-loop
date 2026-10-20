"""The closed-loop controller: one state machine per printer.

    IDLE ──(bed verified empty, job queued)──▶ STARTING ──▶ PRINTING
      ▲                                                        │ EJECT marker
      │                                                        ▼
      │                                                    EJECTING
      │                                                        │ print FINISH
      │          clear                                         ▼
      └─────────────────────────────────────────────────── VERIFYING
                                                     not clear │  ▲
                                                               ▼  │ recovery FINISH
                                                           RECOVERING
                                         retries exhausted / any fault ──▶ BLOCKED

Nothing fails silently. A missing trigger, an ejector that stalls or does not
answer, a camera that sees parts left behind, an HMS fault, a failed print or a
job that never starts all raise an event, stop the queue for that printer and
notify you. A new job is only ever started on a plate the camera has confirmed
empty (or that an operator has explicitly marked as cleared).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .ejector import Ejector, EjectResult
from .printer.state import PrinterState
from .protocol import Marker, MarkerEdgeDetector
from .store import Store
from .vision import BedCheck, BedVerifier

log = logging.getLogger(__name__)


class Phase(StrEnum):
    OFFLINE = "offline"
    IDLE = "idle"
    STARTING = "starting"
    PRINTING = "printing"
    EJECTING = "ejecting"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    BLOCKED = "blocked"


class PrinterLink(Protocol):
    state: PrinterState

    def upload(self, name: str, data: bytes) -> None: ...
    def start(self, name: str, plate: int = 1) -> None: ...
    def start_gcode(self, name: str) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def snapshot(self) -> bytes: ...


class JobRejected(Exception):
    """The job itself is bad (unsupported material, broken file): fail it, keep the printer running."""


@dataclass
class Timing:
    start_timeout_s: float = 240.0
    verify_settle_s: float = 3.0
    offline_after_s: float = 120.0  # no report while printing
    eject_watchdog_s: float = 900.0  # EJECT seen but the print never finished
    snapshot_attempts: int = 3
    snapshot_retry_s: float = 2.0


# Returns (remote file name, file bytes, plate number). Plate 0 means a plain .gcode file.
PrepareJob = Callable[[dict], tuple[str, bytes, int]]
RecoveryJob = Callable[[dict | None], tuple[str, bytes]]


class LoopController:
    def __init__(
        self,
        name: str,
        model: str,
        link: PrinterLink,
        ejector: Ejector,
        store: Store,
        prepare_job: PrepareJob,
        recovery_job: RecoveryJob,
        verifier: BedVerifier | None = None,
        emit: Callable[[dict], None] | None = None,
        quiet_mode: Callable[[], str | None] = lambda: None,
        max_retries: int = 1,
        bend_window_s: float = 25.0,
        quiet_profile: str = "quiet",
        plate_type: str = "textured_pei",
        rated_cycles: int = 1500,
        timing: Timing | None = None,
        clock: Callable[[], float] = time.monotonic,
        run_async: Callable[[Callable[[], None]], None] | None = None,
        marker_base: int = 9000,
        diff_dir: str | Path | None = None,
    ):
        self.name = name
        self.model = model
        self.link = link
        self.ejector = ejector
        self.store = store
        self.prepare_job = prepare_job
        self.recovery_job = recovery_job
        self.verifier = verifier
        self._emit = emit or (lambda e: None)
        self.quiet_mode = quiet_mode
        self.max_retries = max_retries
        self.bend_window_s = bend_window_s
        self.quiet_profile = quiet_profile
        self.plate_type = plate_type
        self.rated_cycles = rated_cycles
        self.t = timing or Timing()
        self.clock = clock
        self.run_async = run_async or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.diff_dir = Path(diff_dir) if diff_dir else None

        self.lock = threading.RLock()
        self.phase = Phase.OFFLINE
        self.online = False
        self._stale_reported = False
        self._unverified_reported = False
        self.enabled = True  # operator switch for this printer's loop
        self.bed_clear = False  # only True after a camera check or an operator confirmation
        self.job: dict | None = None
        self.loop_id: int | None = None
        self.retries = 0
        self.armed = False  # JOB_START seen in the current print
        self.trigger_seen = False
        self.sweep_seen = False
        self.blocked_reason = ""
        self.last_check: BedCheck | None = None
        self.last_eject: EjectResult | None = None
        self._phase_since = clock()
        self._last_report = 0.0
        self._prev_gcode_state = ""
        self._busy = False  # an async step (start, eject, verify) is in flight
        self._paused_by_us = False
        self._printer_pause_reported = False
        self._seen_errors: set[str] = set()
        self._markers = MarkerEdgeDetector(marker_base)
        self._verify_due = 0.0
        self._verify_reason = ""

    # ---- helpers -------------------------------------------------------------
    def _set(self, phase: Phase) -> None:
        if phase != self.phase:
            log.info("%s: %s -> %s", self.name, self.phase.value, phase.value)
            self.phase = phase
            self._phase_since = self.clock()

    def event(self, level: str, kind: str, message: str, data: dict | None = None) -> None:
        e = self.store.add_event(self.name, level, kind, message, data)
        self._emit(e)

    def block(self, reason: str, kind: str = "blocked", pause: bool = False) -> None:
        with self.lock:
            if pause:
                try:
                    self.link.pause()
                    self._paused_by_us = True
                except Exception as e:  # report both problems, never hide the first
                    reason += f" (and pausing the printer failed: {e})"
            if self.loop_id:
                self.store.update_loop(
                    self.loop_id,
                    finished=time.time(),
                    result="blocked",
                    retries=self.retries,
                    trigger_seen=int(self.trigger_seen),
                )
                self.loop_id = None
            self.blocked_reason = reason
            self.bed_clear = False
            self._busy = False
            self._set(Phase.BLOCKED)
            self.event("error", kind, reason)

    @property
    def profile(self) -> str:
        return self.quiet_profile if self.quiet_mode() == "quiet" else "standard"

    # ---- inputs ----------------------------------------------------------------
    def on_state(self, state: PrinterState) -> None:
        with self.lock:
            self._last_report = self.clock()
            if state.online and not self.online:
                self.online = True
                if self.phase == Phase.OFFLINE:
                    self._set(Phase.IDLE)
                self.event("info", "online", "printer connected")
            elif not state.online and self.online:
                self.online = False
                active = self.phase not in (Phase.IDLE, Phase.BLOCKED, Phase.OFFLINE)
                self.event(
                    "error" if active else "warning",
                    "offline",
                    "lost the MQTT connection to the printer; the loop is on hold until it reconnects",
                )
                return
            self._check_firmware(state)
            self._check_errors(state)

            marker = self._markers.feed(state.layer_num)
            if marker is not None:
                self._on_marker(marker)

            gs = state.gcode_state
            if gs != self._prev_gcode_state:
                prev, self._prev_gcode_state = self._prev_gcode_state, gs
                self._on_gcode_state(prev, gs)

    def _check_firmware(self, state: PrinterState) -> None:
        if not state.firmware:
            return
        key = f"firmware:{self.name}"
        known = self.store.get_meta(key)
        if known != state.firmware:
            self.store.set_meta(key, state.firmware)
            if known:
                self.event(
                    "warning",
                    "firmware_changed",
                    f"printer firmware changed from {known} to {state.firmware}; run `clearloop probe-marker` "
                    "and one supervised loop before leaving it unattended",
                )

    def _check_errors(self, state: PrinterState) -> None:
        errors = set(state.blocking_errors)
        new = errors - self._seen_errors
        self._seen_errors = errors
        if new and self.phase in (
            Phase.STARTING,
            Phase.PRINTING,
            Phase.EJECTING,
            Phase.RECOVERING,
            Phase.VERIFYING,
        ):
            self.block("printer fault: " + ", ".join(sorted(new)), kind="printer_fault")

    def _on_marker(self, m: Marker) -> None:
        log.info("%s: marker %s", self.name, m.name)
        if m == Marker.JOB_START:
            self.armed = True
            self.trigger_seen = False
            self.sweep_seen = False
            if self.phase == Phase.IDLE:
                # A ClearLoop file started from the slicer or another tool: follow it.
                self.job = None
                self.loop_id = self.store.start_loop(self.name, None)
                self.bed_clear = False
                self._set(Phase.PRINTING)
        elif m == Marker.DOOR_OPEN:
            self.run_async(self._open_door)
        elif m == Marker.EJECT:
            self.trigger_seen = True
            if self.phase in (Phase.PRINTING, Phase.STARTING):
                self._set(Phase.EJECTING)
            self.run_async(self._run_eject)
        elif m == Marker.SWEEP_DONE:
            self.sweep_seen = True
        elif m == Marker.LOOP_END:
            # Multi-copy file: hold the printer and check the plate before the next copy.
            if self.verifier is not None:
                try:
                    self.link.pause()
                    self._paused_by_us = True
                except Exception as e:
                    self.block(f"could not pause between copies to verify the plate: {e}")
                    return
                self._schedule_verify("between copies")

    def _on_gcode_state(self, prev: str, gs: str) -> None:
        if gs in ("RUNNING", "PREPARE"):
            self._printer_pause_reported = False
            if self.phase == Phase.STARTING:
                self._set(Phase.PRINTING)
            elif self.phase == Phase.IDLE and gs == "RUNNING":
                # Something else started a print. We only learn whether it is a ClearLoop
                # file when JOB_START shows up; either way the plate is no longer known clear.
                self.bed_clear = False
            if self._paused_by_us and prev == "PAUSE":
                self._paused_by_us = False
        elif gs == "PAUSE":
            if not self._paused_by_us and not self._printer_pause_reported:
                self._printer_pause_reported = True
                self.event(
                    "error",
                    "printer_paused",
                    "printer paused itself (filament runout, HMS fault or a user); the loop waits until it resumes",
                )
        elif gs == "FINISH":
            if self.phase in (Phase.PRINTING, Phase.EJECTING, Phase.STARTING):
                if self.armed and not self.trigger_seen and self.ejector.gcode_mode == "board":
                    self.event(
                        "error",
                        "trigger_missed",
                        "print finished but the EJECT marker never arrived; the bender did not run. "
                        "Checking the plate and recovering instead of starting the next job.",
                    )
                self._schedule_verify("after print")
            elif self.phase == Phase.RECOVERING:
                self._schedule_verify("after recovery sweep")
            elif self.phase == Phase.IDLE:
                self.bed_clear = False
        elif gs == "FAILED":
            if self.phase in (Phase.STARTING, Phase.PRINTING, Phase.EJECTING, Phase.RECOVERING):
                if self.job:
                    self.store.finish_job(self.job["id"], "failed", "printer reported FAILED")
                self.block(
                    "print failed or was cancelled on the printer; check the plate", kind="print_failed"
                )

    # ---- timers ----------------------------------------------------------------
    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            if not self.online:
                return
            active = self.phase in (Phase.PRINTING, Phase.EJECTING, Phase.RECOVERING)
            if active and now - self._last_report > self.t.offline_after_s:
                if not self._stale_reported:
                    self._stale_reported = True
                    self.event(
                        "warning",
                        "stale",
                        f"no status report for {self.t.offline_after_s:.0f} s while active",
                    )
            else:
                self._stale_reported = False
            if self._busy:
                return
            if self.phase == Phase.STARTING and now - self._phase_since > self.t.start_timeout_s:
                if self.job:
                    self.store.requeue_job(self.job["id"], "printer did not start")
                self.block("printer did not start the job in time", kind="start_timeout")
            elif self.phase == Phase.EJECTING and now - self._phase_since > self.t.eject_watchdog_s:
                self.block("ejection started but the print never finished", kind="eject_timeout")
            elif self.phase == Phase.VERIFYING and now >= self._verify_due:
                self._busy = True
                self.run_async(self._verify)
            elif self.phase == Phase.IDLE and self.enabled:
                self._idle_tick()

    def _idle_tick(self) -> None:
        if self.quiet_mode() == "hold":
            return
        state = self.link.state
        if state.gcode_state not in ("IDLE", "FINISH", "FAILED", "UNKNOWN") or not state.online:
            return
        job = self.store.next_job(self.name, self.model, state.ams_types)
        if job is None:
            return
        if not self.bed_clear:
            self._schedule_verify("before starting a job", settle=0)
            return
        if not self.store.claim_job(job["id"], self.name):
            return
        self.job = job
        self.retries = 0
        self.armed = self.trigger_seen = self.sweep_seen = False
        self.loop_id = self.store.start_loop(self.name, job["id"])
        self._busy = True
        self._set(Phase.STARTING)
        self.run_async(self._start_job)

    def _schedule_verify(self, reason: str, settle: float | None = None) -> None:
        self._verify_reason = reason
        self._verify_due = self.clock() + (self.t.verify_settle_s if settle is None else settle)
        self._set(Phase.VERIFYING)

    # ---- async steps -------------------------------------------------------------
    def _start_job(self) -> None:
        job = self.job
        assert job is not None
        try:
            prep = self.ejector.prepare()
            if not prep.ok:
                raise RuntimeError(f"ejector not ready: {prep.message}")
            try:
                remote, data, plate = self.prepare_job(job)
            except JobRejected as e:
                with self.lock:
                    self.store.finish_job(job["id"], "failed", str(e))
                    if self.loop_id:
                        self.store.update_loop(self.loop_id, finished=time.time(), result="rejected")
                        self.loop_id = None
                    self.job = None
                    self._busy = False
                    self._set(Phase.IDLE)
                    self.event("error", "job_rejected", f"job {job['id']} ({job['name']}) rejected: {e}")
                return
            self.link.upload(remote, data)
            if plate:
                self.link.start(remote, plate)
            else:
                self.link.start_gcode(remote)
        except Exception as e:
            with self.lock:
                self.store.requeue_job(job["id"], str(e))
                self.job = None
                self.block(f"could not start job {job['id']}: {e}", kind="start_failed")
            return
        with self.lock:
            self.bed_clear = False
            self._busy = False
            self._phase_since = self.clock()
            self.event("info", "job_started", f"started job {job['id']} ({job['name']})", {"job": job["id"]})

    def _open_door(self) -> None:
        r = self.ejector.door(True)
        if not r.ok:
            self.block(f"door did not open: {r.message}", kind="door_failed", pause=True)

    def _run_eject(self) -> None:
        r = self.ejector.eject(self.profile)
        with self.lock:
            self.last_eject = r
            if self.loop_id:
                self.store.update_loop(
                    self.loop_id, eject_s=r.duration_s, peak_current_a=r.peak_current_a, trigger_seen=1
                )
            if not r.ok:
                why = "stalled" if r.stalled else "failed"
                self.block(f"ejector {why}: {r.message}", kind="eject_failed", pause=True)
                return
            if self.ejector.gcode_mode == "board" and r.duration_s > self.bend_window_s:
                self.event(
                    "warning",
                    "eject_slow",
                    f"bend took {r.duration_s:.1f} s but the printer only waits {self.bend_window_s:.0f} s; "
                    "raise bend_window_s",
                    r.as_dict(),
                )

    def _verify(self) -> None:
        reason = self._verify_reason
        if self.verifier is None:
            # Open-loop mode, by explicit configuration only. Say so, once.
            with self.lock:
                self._busy = False
                if not self._unverified_reported:
                    self._unverified_reported = True
                    self.event(
                        "warning",
                        "unverified",
                        "bed verification is disabled for this printer; plates are assumed empty after the sweep",
                    )
                if reason == "before starting a job" or self.sweep_seen or reason == "between copies":
                    self._on_clear(reason)
                else:
                    self.block(
                        f"the sweep never reported done ({reason}) and there is no camera check to "
                        "confirm the plate is empty",
                        kind="plate_unknown",
                    )
            return
        check = None
        error: Exception | None = None
        for attempt in range(self.t.snapshot_attempts):
            try:
                check = self.verifier.check(self.link.snapshot())
                break
            except Exception as e:  # cameras drop frames; retry before giving up
                error = e
                if attempt + 1 < self.t.snapshot_attempts:
                    time.sleep(self.t.snapshot_retry_s)
        with self.lock:
            self._busy = False
            if self.phase != Phase.VERIFYING:
                return  # something else (a fault, an operator) took over meanwhile
            if check is None:
                self.block(f"could not verify the plate ({reason}): {error}", kind="verify_failed")
                return
            self.last_check = check
            if self.diff_dir and check.diff_png:
                self.diff_dir.mkdir(parents=True, exist_ok=True)
                (self.diff_dir / f"{self.name}-last-diff.png").write_bytes(check.diff_png)
            if self.loop_id:
                self.store.update_loop(self.loop_id, changed_fraction=check.changed_fraction)
            if check.clear:
                self._on_clear(reason)
            else:
                self._on_not_clear(reason, check)

    def _on_clear(self, reason: str) -> None:
        self.bed_clear = True
        if reason == "between copies":
            self._set(Phase.PRINTING)
            self.store.plate_cycle(self.name, retried=False)
            self.ejector.plate_used()
            self.link.resume()
            return
        if self.loop_id:
            result = "recovered" if self.retries else "cleared"
            self.store.update_loop(
                self.loop_id,
                finished=time.time(),
                result=result,
                retries=self.retries,
                trigger_seen=int(self.trigger_seen),
            )
            self.loop_id = None
            plate = self.store.plate_cycle(self.name, retried=self.retries > 0)
            self.ejector.plate_used()
            self._check_plate_wear(plate)
        if self.job:
            self.store.finish_job(self.job["id"], "done")
            self.event(
                "info",
                "loop_done",
                f"job {self.job['id']} done, plate verified empty",
                {"retries": self.retries},
            )
        self.job = None
        self.retries = 0
        self.armed = False
        self._set(Phase.IDLE)

    def _on_not_clear(self, reason: str, check: BedCheck) -> None:
        if reason in ("before starting a job", "between copies") or self.retries >= self.max_retries:
            what = (
                "parts are still on the plate"
                if reason != "before starting a job"
                else "the plate is not empty"
            )
            self.block(
                f"{what} ({reason}, {self.retries} recovery attempts); clear it and press 'Plate cleared'",
                kind="plate_not_clear",
            )
            return
        self.retries += 1
        self.event(
            "warning",
            "plate_not_clear",
            f"parts left on the plate {reason}; running recovery sweep {self.retries}/{self.max_retries}",
            check.as_dict(),
        )
        self._busy = True
        self._set(Phase.RECOVERING)
        self.run_async(self._start_recovery)

    def _start_recovery(self) -> None:
        try:
            name, data = self.recovery_job(self.job)
            self.link.upload(name, data)
            self.link.start_gcode(name)
        except Exception as e:
            self.block(f"could not start the recovery sweep: {e}", kind="recovery_failed")
            return
        with self.lock:
            self._busy = False
            self._phase_since = self.clock()

    def _check_plate_wear(self, plate: dict) -> None:
        cycles, rated = plate["cycles"], plate["rated_cycles"]
        if cycles in (int(rated * 0.9), rated):
            self.event(
                "warning",
                "plate_wear",
                f"plate has done {cycles} of {rated} rated cycles; plan a replacement",
                plate,
            )
        recent = self.store.loops(self.name, limit=50)
        done = [r for r in recent if r["result"] in ("cleared", "recovered", "blocked")]
        if len(done) >= 20:
            rate = sum(1 for r in done if r["result"] != "cleared") / len(done)
            if rate > 0.15 and cycles % 10 == 0:
                self.event(
                    "warning",
                    "release_degrading",
                    f"{rate:.0%} of the last {len(done)} loops needed help; the plate may be worn or need cleaning",
                )

    # ---- operator actions -----------------------------------------------------
    def mark_cleared(self) -> None:
        """An operator has removed whatever was on the plate."""
        with self.lock:
            if self._paused_by_us and self.link.state.gcode_state == "PAUSE":
                self.link.resume()
                self._paused_by_us = False
                self.bed_clear = True
                self.blocked_reason = ""
                self._set(Phase.PRINTING)
                self.event("info", "resolved", "operator cleared the plate; resuming the paused print")
                return
            if self.job:
                self.store.finish_job(self.job["id"], "done", "plate cleared by operator")
                self.job = None
            self.bed_clear = True
            self.blocked_reason = ""
            self.retries = 0
            self._busy = False
            self._set(Phase.IDLE)
            self.event("info", "resolved", "operator marked the plate cleared")

    def reverify(self) -> None:
        with self.lock:
            self.blocked_reason = ""
            self._schedule_verify("before starting a job", settle=0)

    def set_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.enabled = enabled
            self.event(
                "info",
                "loop_enabled" if enabled else "loop_disabled",
                "loop resumed" if enabled else "loop paused by operator; the current print continues",
            )

    def status(self) -> dict:
        with self.lock:
            return {
                "name": self.name,
                "model": self.model,
                "phase": self.phase.value,
                "since_s": round(self.clock() - self._phase_since, 1),
                "enabled": self.enabled,
                "online": self.online,
                "bed_clear": self.bed_clear,
                "blocked_reason": self.blocked_reason,
                "job": self.job,
                "retries": self.retries,
                "trigger_seen": self.trigger_seen,
                "quiet": self.quiet_mode(),
                "last_check": self.last_check.as_dict() if self.last_check else None,
                "last_eject": self.last_eject.as_dict() if self.last_eject else None,
                "printer": self.link.state.as_dict(),
                "ejector": self.ejector.status(),
                "plate": self.store.active_plate(self.name, self.plate_type, self.rated_cycles),
            }
