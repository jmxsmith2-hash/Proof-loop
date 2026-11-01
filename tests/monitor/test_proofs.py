"""Proof photos: a picture of each loop's finished plate before the sweep, kept per job, shown with each order."""

import json
import time

from fastapi.testclient import TestClient
from PIL import Image

from proofloop.app.loop_server import create_app
from proofloop.core.settings import CameraSettings
from proofloop.monitor import proofs as proofs_mod
from proofloop.monitor import watch as watch_mod
from proofloop.monitor.proofs import ProofPhotos
from proofloop.monitor.store import Store
from proofloop.monitor.watch import PrinterWatch
from tests.monitor.test_monitor import Verifier
from tests.monitor.test_orders import take as take_order
from tests.monitor.test_queue import farm
from tests.monitor.test_timelapse import CameraLink, picture

HOT, OFF = 220.0, 0.0


class Clock:
    """The watch's clock, so a test can let a minute pass (a Stage 1 loop is new only a minute after the last)."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


def watching(tmp_path, job="FL_S1_bracket", total=50, on=True, keep=500, run_async=None, verifier=None):
    cfg = CameraSettings(proof_photos=on, proof_keep=keep)
    link = CameraLink(total)
    link.state.subtask_name = job
    ph = ProofPhotos(tmp_path / "proofs", lambda: cfg)
    w = PrinterWatch("p2s", link, verifier or Verifier([True] * 5), Store(), settle_s=0,
                     run_async=run_async or (lambda f: f()), retry_s=0, proofs=ph)
    return w, link, ph, cfg


def to(w, link, **kw):
    for k, v in kw.items():
        setattr(link.state, k, v)
    w.on_state(link.state)


def print_loop(w, link, total=50, first=1):
    for layer in range(first, total + 1):
        to(w, link, gcode_state="RUNNING", layer_num=layer, nozzle_target=HOT)


# ---- when the picture is taken ------------------------------------------------------------------------
def test_a_stage_1_loop_is_photographed_once_its_heaters_go_off_and_never_again_in_the_sweep(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(watch_mod, "time", clock)
    w, link, ph, _ = watching(tmp_path)
    print_loop(w, link)
    assert ph.listing() == [] and link.taken == 0  # the last layer is still printing
    to(w, link, nozzle_target=OFF)  # the end-of-print cycle: heaters off, the plate cools
    (first,) = ph.listing()
    assert first["loop"] == 1 and first["job"] == "FL_S1_bracket" and first["printer"] == "p2s"
    for _ in range(3):  # the cooldown, the sweep and the loop-end pause all still report the last layer
        to(w, link, bed_target=30.0)
    to(w, link, gcode_state="PAUSE")
    to(w, link, gcode_state="RUNNING", nozzle_target=HOT)  # resumed: the next loop's start G-code heats up
    assert len(ph.listing()) == 1 and link.taken == 2  # the proof photo, then the plate check's picture
    clock.now += 600
    print_loop(w, link)  # the layers start over: loop 2
    to(w, link, nozzle_target=OFF)
    assert w.loop_no == 2 and [p["loop"] for p in ph.listing()] == [2, 1]


def test_a_farmboard_loop_is_photographed_at_print_done_before_the_bend(tmp_path):
    w, link, ph, _ = watching(tmp_path, job="FL_S2_bracket")
    for loop in (1, 2):
        to(w, link, layer_num=0, nozzle_target=1.0)  # FarmBoard reset: print starting
        print_loop(w, link)
        to(w, link, nozzle_target=2.0)  # print done: door opening, cooling
        assert len(ph.listing()) == loop
        for code in (3.0, 4.0, 5.0):  # bending the plate, safety check, door closing
            to(w, link, nozzle_target=code)
        to(w, link, gcode_state="PAUSE")
        assert len(ph.listing()) == loop
    assert [p["loop"] for p in ph.listing()] == [2, 1] and link.taken == 4  # and a plate check after each


def test_no_picture_of_a_loop_only_seen_at_its_end_or_one_that_failed(tmp_path):
    w, link, ph, _ = watching(tmp_path)
    to(w, link, layer_num=50, nozzle_target=OFF)  # ProofLoop started during the cooldown, or the sweep
    to(w, link, layer_num=50, nozzle_target=3.0)
    assert ph.listing() == []
    w, link, ph, _ = watching(tmp_path / "failed")
    print_loop(w, link, total=20)  # 20 of its 50 layers
    to(w, link, gcode_state="FAILED", nozzle_target=OFF)
    assert ph.listing() == [] and link.taken == 0


def test_no_picture_once_the_bend_has_started(tmp_path):
    w, link, ph, _ = watching(tmp_path, job="FL_S2_bracket")
    to(w, link, nozzle_target=1.0)
    print_loop(w, link)
    to(w, link, nozzle_target=3.0)  # the "print done" signal was missed; the plate is being bent
    to(w, link, nozzle_target=OFF)
    assert ph.listing() == []


def test_only_loop_files_and_only_when_turned_on(tmp_path):
    w, link, ph, _ = watching(tmp_path, job="bracket")
    print_loop(w, link)
    to(w, link, nozzle_target=OFF)
    assert ph.listing() == []
    w, link, ph, cfg = watching(tmp_path / "off", on=False)
    print_loop(w, link)
    to(w, link, nozzle_target=OFF)
    assert ph.listing() == [] and link.taken == 0 and not ph.dir.exists()


def test_the_picture_is_taken_off_the_printers_update(tmp_path):
    later = []
    w, link, ph, _ = watching(tmp_path, run_async=later.append)
    print_loop(w, link)
    to(w, link, nozzle_target=OFF)
    assert link.taken == 0 and ph.listing() == [] and len(later) == 1
    later.pop()()
    assert link.taken == 1 and len(ph.listing()) == 1


def test_the_picture_waits_for_the_toolhead_to_park(tmp_path):
    later = []
    w, link, ph, _ = watching(tmp_path, run_async=later.append)
    w.settle_s = 0.2  # the plate check's settle time
    print_loop(w, link)
    to(w, link, nozzle_target=OFF)
    start = time.monotonic()
    later.pop()()
    assert time.monotonic() - start >= 0.2 and len(ph.listing()) == 1


def test_a_camera_that_fails_skips_the_picture_and_the_plate_check_goes_on(tmp_path):
    w, link, ph, _ = watching(tmp_path)
    link.broken = lambda n: True
    print_loop(w, link)
    to(w, link, nozzle_target=OFF)
    to(w, link, nozzle_target=OFF, bed_target=30.0)
    assert ph.listing() == [] and link.taken == 1  # once a loop, not again with every update
    link.broken = lambda n: False
    to(w, link, gcode_state="PAUSE")
    assert w.last_check is not None and w.last_check.clear and link.resumed == 1


def test_the_card_shows_the_printers_latest_few(tmp_path):
    w, link, ph, _ = watching(tmp_path)
    for loop in range(1, 7):
        ph.take("p2s", "FL_S1_bracket", loop, lambda: picture(1), lambda f: f())
    ph.take("other", "FL_S1_bracket", 9, lambda: picture(1), lambda f: f())
    card = w.as_dict()["proofs"]
    assert [p["loop"] for p in card] == [6, 5, 4, 3] and len(card) == proofs_mod.ON_CARD


# ---- what is kept, and where --------------------------------------------------------------------------
def test_pictures_are_small_jpegs_in_a_folder_for_the_job(tmp_path):
    ph = ProofPhotos(tmp_path / "proofs", CameraSettings)
    assert ph.take("p2s", "FL_S2_bracket_x3.gcode.3mf", 3, lambda: picture(1, (1920, 1080)), lambda f: f())
    (item,) = ph.listing()
    path = ph.file(item["id"])
    assert path.parent == tmp_path / "proofs" / "FL_S2_bracket_x3" and "-loop3-" in path.name
    with Image.open(path) as im:
        assert im.format == "JPEG" and im.size == (proofs_mod.WIDTH, 360)
    assert path.stat().st_size < 120_000
    note = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert note == item and item["job"] == "FL_S2_bracket_x3"


def test_only_the_newest_are_kept_and_empty_job_folders_go(tmp_path):
    cfg = CameraSettings(proof_keep=10)
    ph = ProofPhotos(tmp_path / "proofs", lambda: cfg)
    ph.take("p2s", "FL_S1_old", 1, lambda: picture(1), lambda f: f())
    time.sleep(0.01)
    for loop in range(1, 13):
        ph.take("p2s", "FL_S1_new", loop, lambda n=loop: picture(n), lambda f: f())
    kept = ph.listing()
    assert len(kept) == 10 and [p["loop"] for p in kept] == list(range(12, 2, -1))
    assert not (ph.dir / "FL_S1_old").exists()
    assert len(list((ph.dir / "FL_S1_new").glob("*.jpg"))) == 10 == len(list((ph.dir / "FL_S1_new").glob("*.json")))


def test_what_is_kept_is_read_back_after_a_restart(tmp_path):
    ph = ProofPhotos(tmp_path / "proofs", CameraSettings)
    for loop in (1, 2):
        ph.take("p2s", "FL_S1_bracket", loop, lambda n=loop: picture(n), lambda f: f())
        time.sleep(0.02)  # taken one after the other, as loops are
    (ph.dir / "FL_S1_bracket" / "notes.json").write_text("{}", encoding="utf-8")  # not one of ours
    (ph.dir / "junk").mkdir()
    (ph.dir / "junk" / "20260101-101010-loop1-abcdef.json").write_text("not json", encoding="utf-8")
    again = ProofPhotos(tmp_path / "proofs", CameraSettings)
    assert again.listing() == ph.listing() and [p["loop"] for p in again.listing()] == [2, 1]


def test_a_picture_is_found_only_by_its_own_id(tmp_path):
    ph = ProofPhotos(tmp_path / "proofs", CameraSettings)
    ph.take("p2s", "FL_S1_bracket", 1, lambda: picture(1), lambda f: f())
    pid = ph.listing()[0]["id"]
    assert ph.file(pid) is not None
    for bad in ("../x", pid + "x", "20200101-000000-loop1-abcdef", "", f"FL_S1_bracket/{pid}"):
        assert ph.file(bad) is None, bad


def test_an_orders_pictures_are_its_jobs_files_on_their_printers_while_they_ran(tmp_path):
    ph = ProofPhotos(tmp_path / "proofs", CameraSettings)

    def shot(printer, job, loop):
        ph.take(printer, job, loop, lambda: picture(loop), lambda f: f())

    shot("a", "FL_S1_bracket_x2", 1)  # before the job was sent: an earlier run of the same file
    time.sleep(0.05)
    jobs = [{"printer": "a", "file": "FL_S1_bracket_x2.gcode.3mf", "at": time.time(), "ended": None},
            {"printer": "b", "file": None, "at": None}]  # still waiting
    time.sleep(0.05)
    shot("a", "FL_S1_bracket_x2", 1)
    shot("a", "FL_S1_bracket_x2", 2)
    shot("b", "FL_S1_bracket_x2", 1)  # another printer printing the same file for someone else
    shot("a", "FL_S1_other", 1)
    got = ph.for_jobs(jobs)
    assert [(p["printer"], p["loop"]) for p in got] == [("a", 1), ("a", 2)] and got[0]["taken"] >= jobs[0]["at"]
    jobs[0]["ended"] = time.time() - 3600  # it ended long before these were taken
    assert ph.for_jobs(jobs) == []


# ---- over the API -------------------------------------------------------------------------------------
def test_each_order_shows_its_gallery_and_the_pictures_are_jpegs(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    o = take_order(c, quantity=4).json()
    assert c.get("/api/orders").json()[0]["proofs"] == []
    run = app.queue.get(o["run"])
    job = run["jobs"][0]
    app.queue.mark(run["id"], job["id"], status="printing", file="FL_S1_bracket_x2.gcode.3mf", at=time.time())
    app.proofs.take("a", "FL_S1_bracket_x2", 1, lambda: picture(1), lambda f: f())
    (shown,) = c.get("/api/orders").json()[0]["proofs"]
    assert shown["loop"] == 1 and shown["printer"] == "a"
    r = c.get(f"/api/proofs/{shown['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert c.get("/api/state").json()["monitor"][0]["proofs"][0]["id"] == shown["id"]  # and on the printer's card
    for bad in ("nothing", "..%2F..%2Fsettings.json", "20200101-000000-loop1-abcdef"):
        assert c.get(f"/api/proofs/{bad}").status_code == 404
    app.settings.app_token = "secret"
    assert c.get(f"/api/proofs/{shown['id']}").status_code == 401
    assert c.get(f"/api/proofs/{shown['id']}?token=secret").status_code == 200  # how the page's <img> asks
    app.shutdown()


def test_the_proof_photo_settings_are_kept(tmp_path):
    app = farm(tmp_path, "a")
    c = TestClient(create_app(app))
    r = c.put("/api/camera-settings", json={"proof_photos": False, "proof_keep": 50})
    assert r.status_code == 200 and r.json()["proof_photos"] is False
    assert not app.proofs.take("a", "FL_S1_x", 1, lambda: picture(1), lambda f: f())  # the running App follows it
    app.shutdown()
