from clearloop.monitor import PrinterWatch
from clearloop.printer.state import HmsEvent, PrinterState
from clearloop.store import Store
from clearloop.vision import BedCheck


class Link:
    def __init__(self):
        self.state = PrinterState(
            online=True, gcode_state="RUNNING", subtask_name="FL_S2_bucket", total_layers=850, layer_num=10
        )
        self.resumed = 0

    def resume(self):
        self.resumed += 1
        return True

    def snapshot(self):
        return b"jpeg"


class Verifier:
    def __init__(self, results, refs=1):
        self.results = list(results)
        self.references = ["ref"] * refs

    def check(self, jpeg, want_diff=True):
        clear = self.results.pop(0)
        return BedCheck(clear, 0.0 if clear else 0.05, 0 if clear else 500, "ref-00.jpg")


def watch(results=(True,), refs=1, auto=True):
    link = Link()
    w = PrinterWatch(
        "p2s",
        link,
        Verifier(results, refs),
        Store(),
        auto_resume=auto,
        settle_s=0,
        run_async=lambda f: f(),
        retry_s=0,
    )
    w.on_state(link.state)
    return w, link


def to(w, link, **kw):
    for k, v in kw.items():
        setattr(link.state, k, v)
    w.on_state(link.state)


def test_loop_end_pause_resumes_only_on_an_empty_plate():
    w, link = watch([True])
    to(w, link, layer_num=850, gcode_state="PAUSE")
    assert link.resumed == 1 and not w.waiting_for_you
    w, link = watch([False])
    to(w, link, layer_num=850, gcode_state="PAUSE")
    assert link.resumed == 0 and w.waiting_for_you
    assert w.store.events()[0]["kind"] == "plate_not_clear"


def test_never_resumes_without_reference_pictures():
    w, link = watch([True], refs=0)
    to(w, link, layer_num=850, gcode_state="PAUSE")
    assert link.resumed == 0 and w.waiting_for_you


def test_other_pauses_are_left_alone():
    w, link = watch([True])
    to(w, link, layer_num=400, gcode_state="PAUSE")  # filament runout mid-print
    assert link.resumed == 0
    w, link = watch([True])
    to(w, link, subtask_name="FL_S1_CALIBRATION-sweep-floor_x", layer_num=850, gcode_state="PAUSE")
    assert link.resumed == 0
    w, link = watch([True])
    to(w, link, subtask_name="", gcode_file="/sdcard/FL_S2_x.gcode.3mf", layer_num=850, gcode_state="PAUSE")
    assert link.resumed == 1  # recognised from the file path too


def test_check_again_after_clearing():
    w, link = watch([False, True])
    to(w, link, layer_num=850, gcode_state="PAUSE")
    assert link.resumed == 0
    w.check_again()
    assert link.resumed == 1 and not w.waiting_for_you


def test_sentinels_and_hms_become_events():
    w, link = watch()
    to(w, link, nozzle_target=2.0)
    to(w, link, hms=[HmsEvent(0x05000500, 0x00010007)])
    kinds = [e["message"] for e in w.store.events()]
    assert any("door opening" in m for m in kinds)
    assert "Developer Mode" in w.status
