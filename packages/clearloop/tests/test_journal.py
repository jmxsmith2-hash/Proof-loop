from test_monitor import Verifier, to

from clearloop.journal import CANCELLED_ERROR, PrintJournal
from clearloop.monitor import PrinterWatch
from clearloop.printer.state import HmsEvent, PrinterState
from clearloop.store import Store


class Link:
    def __init__(self, job="plate_A"):
        self.state = PrinterState(online=True, gcode_state="IDLE", subtask_name=job, total_layers=120)
        self.resumed = 0
        self.pictures = 0

    def resume(self):
        self.resumed += 1
        return True

    def snapshot(self):
        self.pictures += 1
        return b"jpeg"


def watched(tmp_path, results=(True,), job="plate_A"):
    store = Store()
    journal = PrintJournal(store, tmp_path, {"p2s": "N7"})
    link = Link(job)
    w = PrinterWatch("p2s", link, Verifier(results), store, auto_resume=True, settle_s=0,
                     run_async=lambda f: f(), retry_s=0, journal=journal)
    w.on_state(link.state)
    return w, link, store, journal


def test_a_finished_print_is_recorded_with_its_temperatures_and_plate_check(tmp_path):
    w, link, store, _ = watched(tmp_path)
    to(w, link, gcode_state="RUNNING", layer_num=1, nozzle_target=220.0, bed_target=55.0)
    to(w, link, layer_num=60)
    to(w, link, layer_num=120, gcode_state="FINISH")
    (rec,) = store.outcomes()
    assert rec["outcome"] == "finished" and rec["layer"] == 120 and rec["model_id"] == "N7"
    assert rec["nozzle_target_max"] == 220 and rec["bed_target_max"] == 55
    assert rec["eject"] == "clear"                                   # the plate check after FINISH


def test_a_failure_keeps_layer_hms_and_a_picture_but_a_cancel_is_not_a_failure(tmp_path):
    w, link, store, _ = watched(tmp_path)
    to(w, link, gcode_state="RUNNING", layer_num=3, nozzle_target=220.0)
    link.state.hms = [HmsEvent(0x0C000300, 0x0003000B)]
    to(w, link, layer_num=4)
    to(w, link, gcode_state="FAILED", print_error=0x0C00800B)
    rec = store.outcomes()[0]
    assert rec["outcome"] == "failed" and rec["layer"] == 4 and rec["print_error"] == "0C00800B"
    assert rec["hms"] == [{"code": "0C00-0300-0003-000B", "level": "notification"}]
    assert rec["picture"] and link.pictures >= 1
    w, link, store, _ = watched(tmp_path, job="plate_B")
    to(w, link, gcode_state="RUNNING", layer_num=10)
    to(w, link, gcode_state="FAILED", print_error=CANCELLED_ERROR)
    assert store.outcomes()[0]["outcome"] == "cancelled"


def test_each_loop_of_a_loop_file_is_its_own_record_and_parts_left_are_noted(tmp_path):
    w, link, store, journal = watched(tmp_path, results=[False], job="FL_S2_plate")
    journal.keep_file("FL_S2_plate.gcode.3mf", b"zip")
    to(w, link, gcode_state="RUNNING", layer_num=1, nozzle_target=1.0)          # FarmBoard reset, loop 1
    to(w, link, nozzle_target=220.0, layer_num=120)
    to(w, link, nozzle_target=2.0)                                               # print done: T1
    to(w, link, nozzle_target=5.0)
    to(w, link, nozzle_target=1.0, layer_num=1)                                  # loop 2 starts
    to(w, link, nozzle_target=220.0, layer_num=50)
    to(w, link, gcode_state="PAUSE", layer_num=120, nozzle_target=2.0)           # loop 2 done, plate check
    recs = sorted(store.outcomes(), key=lambda r: r["loop_no"])
    assert [(r["loop_no"], r["outcome"]) for r in recs] == [(1, "finished"), (2, "finished")]
    assert all(r["loop_file"] and r["file"].endswith("FL_S2_plate.gcode.3mf") for r in recs)
    assert recs[-1]["eject"] == "parts_left" and recs[-1]["eject_picture"]
    assert recs[0]["nozzle_target_max"] == 220                                   # sentinels are not heat


def test_kept_files_are_pruned(tmp_path):
    from clearloop import journal as j

    jr = PrintJournal(None, tmp_path)
    old = j.KEEP_FILES
    j.KEEP_FILES = 3
    try:
        for k in range(5):
            jr.keep_file(f"FL_S2_{k}.gcode.3mf", b"x")
    finally:
        j.KEEP_FILES = old
    assert len(list((tmp_path / "files").glob("*.gcode.3mf"))) == 3


def test_api_lists_outcomes_and_serves_their_pictures(tmp_path):
    from test_server import client

    c, app = client(tmp_path)
    rid = app.store.add_outcome({"printer": "p2s", "job": "plate_A", "outcome": "failed", "layer": 4,
                                 "ended": 1.0, "file": "/elsewhere/secret.gcode.3mf"})
    pic = app.data_dir / "outcomes" / f"{rid}-failed.jpg"
    pic.parent.mkdir(parents=True)
    pic.write_bytes(b"jpeg")
    data = app.store.outcome(rid)
    data["picture"] = str(pic)
    app.store.update_outcome(rid, data)
    (row,) = c.get("/api/outcomes").json()
    assert row["outcome"] == "failed" and row["has_picture"] and "file" not in row and "picture" not in row
    assert c.get(f"/api/outcomes/{rid}/picture").content == b"jpeg"
    assert c.get(f"/api/outcomes/{rid}/file").status_code == 404           # outside ClearLoop's files folder
