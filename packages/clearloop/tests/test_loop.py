"""The closed-loop state machine, driven by a fake printer."""

from __future__ import annotations

import pytest

from clearloop.ejector import Ejector, EjectResult, GcodeOnlyEjector
from clearloop.loop import JobRejected, LoopController, Phase, Timing
from clearloop.printer.state import PrinterState
from clearloop.protocol import DEFAULT_BASE, Marker
from clearloop.store import Store
from clearloop.vision import BedCheck


class FakeLink:
    def __init__(self):
        self.state = PrinterState(gcode_state="IDLE", online=True)
        self.calls: list[tuple] = []

    def upload(self, name, data):
        self.calls.append(("upload", name))

    def start(self, name, plate=1):
        self.calls.append(("start", name, plate))

    def start_gcode(self, name):
        self.calls.append(("start_gcode", name))

    def pause(self):
        self.calls.append(("pause",))

    def resume(self):
        self.calls.append(("resume",))

    def stop(self):
        self.calls.append(("stop",))

    def snapshot(self):
        return b"jpeg"

    def names(self):
        return [c[0] for c in self.calls]


class FakeVerifier:
    def __init__(self, results):
        self.results = list(results)

    def check(self, jpeg, want_diff=True):
        clear = self.results.pop(0)
        return BedCheck(clear, 0.0 if clear else 0.05, 0 if clear else 400, "ref-00.jpg")


class BoardEjector(Ejector):
    kind = "fake"
    gcode_mode = "board"

    def __init__(self, ok=True, duration=10.0):
        self.ok, self.duration = ok, duration
        self.calls: list[str] = []

    def prepare(self):
        self.calls.append("prepare")
        return EjectResult(True)

    def eject(self, profile="standard"):
        self.calls.append(f"eject:{profile}")
        return EjectResult(
            self.ok, "stalled at 40 mm" if not self.ok else "", self.duration, 1.2, not self.ok
        )

    def door(self, open_):
        self.calls.append(f"door:{open_}")
        return EjectResult(True)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    def make(
        verify=(True, True), ejector=None, prepare=None, quiet=lambda: None, max_retries=1, verifier="fake"
    ):
        store = Store()
        link = FakeLink()
        clock = Clock()
        events = []
        ctl = LoopController(
            name="p1",
            model="p1s",
            link=link,
            ejector=ejector or BoardEjector(),
            store=store,
            prepare_job=prepare or (lambda job: (f"cl-{job['id']}.gcode.3mf", b"3mf", 1)),
            recovery_job=lambda job: ("cl-recovery.gcode", b"sweep"),
            verifier=FakeVerifier(verify) if verifier == "fake" else verifier,
            emit=events.append,
            quiet_mode=quiet,
            max_retries=max_retries,
            timing=Timing(verify_settle_s=0),
            clock=clock,
            run_async=lambda fn: fn(),
        )
        ctl.on_state(link.state)
        return ctl, link, store, clock, events

    return make


def report(ctl, link, gcode_state=None, marker=None, **fields):
    if gcode_state:
        link.state.gcode_state = gcode_state
    if marker is not None:
        link.state.layer_num = DEFAULT_BASE + int(marker)
    for k, v in fields.items():
        setattr(link.state, k, v)
    ctl.on_state(link.state)


def ticks(ctl, n):
    for _ in range(n):
        ctl.tick()


def kinds(events):
    return [e["kind"] for e in events]


def run_print(ctl, link, eject=True):
    report(ctl, link, "PREPARE", Marker.JOB_START)
    report(ctl, link, "RUNNING", layer_num=1)
    report(ctl, link, layer_num=2)
    if eject:
        report(ctl, link, marker=Marker.EJECT)
        report(ctl, link, marker=Marker.SWEEP_DONE)
    report(ctl, link, "FINISH")


def test_happy_path_verifies_before_and_after(env):
    ctl, link, store, clock, events = env()
    store.add_job("cube", "/x.3mf")
    assert ctl.phase == Phase.IDLE

    ctl.tick()  # plate state unknown at start-up: check it before the first job
    assert ctl.phase == Phase.VERIFYING
    ctl.tick()
    assert ctl.bed_clear and ctl.phase == Phase.IDLE

    ctl.tick()
    assert ctl.phase == Phase.STARTING
    assert link.names() == ["upload", "start"]
    assert ctl.ejector.calls == ["prepare"]  # bender retracted before homing

    run_print(ctl, link)
    assert ctl.ejector.calls[-1] == "eject:standard"
    assert ctl.phase == Phase.VERIFYING
    ctl.tick()
    assert ctl.phase == Phase.IDLE
    assert store.jobs()[0]["status"] == "done"
    loop = store.loops()[0]
    assert loop["result"] == "cleared" and loop["trigger_seen"] == 1
    assert store.active_plate("p1")["cycles"] == 1
    assert "loop_done" in kinds(events)


def test_missed_trigger_is_never_silent_and_recovers(env):
    ctl, link, store, clock, events = env(verify=(True, False, True))
    store.add_job("cube", "/x.3mf")
    ticks(ctl, 3)
    run_print(ctl, link, eject=False)
    assert "trigger_missed" in kinds(events)
    ctl.tick()  # parts still there
    assert ctl.phase == Phase.RECOVERING
    assert link.calls[-1] == ("start_gcode", "cl-recovery.gcode")
    report(ctl, link, "RUNNING")
    report(ctl, link, marker=Marker.EJECT)
    report(ctl, link, "FINISH")
    ctl.tick()
    assert ctl.phase == Phase.IDLE
    assert store.loops()[0]["result"] == "recovered"
    assert store.active_plate("p1")["retries"] == 1


def test_parts_left_after_retries_blocks_the_printer(env):
    ctl, link, store, clock, events = env(verify=(True, False, False))
    store.add_job("cube", "/x.3mf")
    store.add_job("cube2", "/x.3mf")
    ticks(ctl, 3)
    run_print(ctl, link)
    ctl.tick()
    report(ctl, link, "RUNNING")
    report(ctl, link, "FINISH")
    ctl.tick()
    assert ctl.phase == Phase.BLOCKED
    assert "still on the plate" in ctl.blocked_reason
    ctl.tick()
    assert ctl.phase == Phase.BLOCKED  # nothing new starts on a dirty plate
    assert [j["status"] for j in store.jobs()] == ["queued", "printing"]

    ctl.mark_cleared()
    assert ctl.phase == Phase.IDLE and ctl.bed_clear
    ctl.tick()
    assert ctl.phase == Phase.STARTING


def test_ejector_stall_pauses_printer_and_blocks(env):
    ctl, link, store, clock, events = env(ejector=BoardEjector(ok=False))
    store.add_job("cube", "/x.3mf")
    ticks(ctl, 3)
    report(ctl, link, "RUNNING", Marker.JOB_START)
    report(ctl, link, marker=Marker.EJECT)
    assert ("pause",) in link.calls
    assert ctl.phase == Phase.BLOCKED
    assert "stalled" in ctl.blocked_reason
    assert events[-1]["level"] == "error"


def test_slow_bend_warns(env):
    ctl, link, store, clock, events = env(ejector=BoardEjector(duration=40))
    store.add_job("cube", "/x.3mf")
    ticks(ctl, 3)
    run_print(ctl, link)
    assert "eject_slow" in kinds(events)


def test_hms_fault_blocks(env):
    ctl, link, store, clock, events = env()
    store.add_job("cube", "/x.3mf")
    ticks(ctl, 3)
    report(ctl, link, "RUNNING", Marker.JOB_START)
    from clearloop.printer.state import HmsEvent

    report(ctl, link, hms=[HmsEvent(0x07002000, 0x00020001)])
    assert ctl.phase == Phase.BLOCKED
    assert "0700_2000_0002_0001" in ctl.blocked_reason


def test_dirty_plate_before_first_job_blocks(env):
    ctl, link, store, clock, events = env(verify=(False,))
    store.add_job("cube", "/x.3mf")
    ticks(ctl, 2)
    assert ctl.phase == Phase.BLOCKED
    assert link.calls == []


def test_rejected_job_fails_but_printer_keeps_going(env):
    def prepare(job):
        raise JobRejected("TPU is not supported")

    ctl, link, store, clock, events = env(prepare=prepare)
    store.add_job("flex", "/x.3mf")
    ticks(ctl, 3)
    assert ctl.phase == Phase.IDLE
    assert store.jobs()[0]["status"] == "failed"
    assert "job_rejected" in kinds(events)


def test_start_timeout(env):
    ctl, link, store, clock, events = env()
    store.add_job("cube", "/x.3mf")
    ticks(ctl, 3)
    clock.t += 1000
    ctl.tick()
    assert ctl.phase == Phase.BLOCKED
    assert store.jobs()[0]["status"] == "queued"  # handed back to the queue


def test_quiet_hours(env):
    mode = {"v": "quiet"}
    ctl, link, store, clock, events = env(quiet=lambda: mode["v"])
    store.add_job("cube", "/x.3mf")
    ticks(ctl, 3)
    run_print(ctl, link)
    assert "eject:quiet" in ctl.ejector.calls
    ctl.tick()
    mode["v"] = "hold"
    store.add_job("cube2", "/x.3mf")
    ctl.tick()
    assert ctl.phase == Phase.IDLE  # held overnight
    mode["v"] = None
    ctl.tick()
    assert ctl.phase == Phase.STARTING


def test_multi_copy_file_is_checked_between_copies(env):
    ctl, link, store, clock, events = env(verify=(True, True, False))
    store.add_job("cubes x3", "/x.3mf")
    ticks(ctl, 3)
    report(ctl, link, "RUNNING", Marker.JOB_START)
    report(ctl, link, marker=Marker.EJECT)
    report(ctl, link, marker=Marker.SWEEP_DONE)
    report(ctl, link, marker=Marker.LOOP_END)
    assert link.calls[-1] == ("pause",)
    report(ctl, link, "PAUSE")
    ctl.tick()
    assert link.calls[-1] == ("resume",)
    report(ctl, link, "RUNNING", Marker.JOB_START)
    report(ctl, link, marker=Marker.EJECT)
    report(ctl, link, marker=Marker.LOOP_END)
    report(ctl, link, "PAUSE")
    ctl.tick()
    assert ctl.phase == Phase.BLOCKED  # second copy left something behind
    assert "printer_paused" not in kinds(events)  # our own pause is not reported as a fault
    ctl.mark_cleared()
    assert link.calls[-1] == ("resume",)


def test_printer_pausing_itself_is_reported(env):
    ctl, link, store, clock, events = env()
    store.add_job("cube", "/x.3mf")
    ticks(ctl, 3)
    report(ctl, link, "RUNNING", Marker.JOB_START)
    report(ctl, link, "PAUSE")
    assert "printer_paused" in kinds(events)


def test_external_clearloop_print_is_followed(env):
    ctl, link, store, clock, events = env(verify=(True,))
    report(ctl, link, "RUNNING", Marker.JOB_START)
    assert ctl.phase == Phase.PRINTING
    report(ctl, link, marker=Marker.EJECT)
    report(ctl, link, "FINISH")
    ctl.tick()
    assert ctl.phase == Phase.IDLE and ctl.bed_clear


def test_disconnect_holds_and_reconnect_resumes(env):
    ctl, link, store, clock, events = env()
    report(ctl, link, online=False)
    assert "offline" in kinds(events)
    store.add_job("cube", "/x.3mf")
    ctl.tick()
    assert link.calls == []
    report(ctl, link, online=True)
    ticks(ctl, 3)
    assert ctl.phase == Phase.STARTING


def test_open_loop_mode_requires_sweep_done(env):
    ctl, link, store, clock, events = env(ejector=GcodeOnlyEjector(), verifier=None)
    store.add_job("cube", "/x.3mf")
    store.add_job("cube", "/x.3mf")
    ticks(ctl, 3)
    run_print(ctl, link)
    ctl.tick()
    assert ctl.phase == Phase.IDLE
    assert "unverified" in kinds(events)
    ctl.tick()
    assert ctl.phase == Phase.STARTING
    report(ctl, link, "RUNNING", Marker.JOB_START)
    report(ctl, link, "FINISH")  # no SWEEP_DONE and no camera: refuse to guess
    ctl.tick()
    assert ctl.phase == Phase.BLOCKED


def test_firmware_change_warns(env):
    ctl, link, store, clock, events = env()
    report(ctl, link, firmware="01.08.02.00")
    assert "firmware_changed" not in kinds(events)
    report(ctl, link, firmware="01.09.00.00")
    assert "firmware_changed" in kinds(events)


class FlakyCamera(FakeLink):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def snapshot(self):
        if self.failures:
            self.failures -= 1
            raise OSError("frame dropped")
        return b"jpeg"


def test_camera_glitch_is_retried_and_fault_during_check_wins(env):
    ctl, link, store, clock, events = env(verify=(True, True))
    ctl.t.snapshot_retry_s = 0
    flaky = FlakyCamera(2)
    flaky.state = link.state
    ctl.link = flaky
    store.add_job("cube", "/x.3mf")
    ticks(ctl, 2)
    assert ctl.bed_clear  # two dropped frames, third one checked

    # A verification that completes after the printer was blocked must not unblock it.
    ctl._schedule_verify("after print", settle=0)
    ctl.block("printer fault: test", kind="printer_fault")
    ctl._verify()
    assert ctl.phase == Phase.BLOCKED
