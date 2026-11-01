"""A run carries on across restarts: loops that end while ProofLoop is stopped are still counted, once."""

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import PrinterConfig
from proofloop.monitor.service import App
from tests.app.test_loop_server import FakeLink, up
from tests.monitor.test_filament import tray
from tests.plates import BUCKET, plain

JOB = "FL_S2_bucket"  # 3 loops of 2 parts, 12.5 g of white PLA each


def start(tmp_path, first=False):
    """ProofLoop starting (again) on the same folder."""
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    if first:
        app.add_printer(PrinterConfig(name="p2s", host="1.2.3.4", serial="S", access_code="x", model_id="N7"))
    w = app.watches["p2s"]
    w.link.state.ams.merge_print({"ams": {"tray_now": "0", "ams": [{"id": "0", "tray": [tray(0)]}]}})
    return app, w, TestClient(create_app(app))


def made(tmp_path):
    """A 3-loop file made, and a spool in the AMS for it."""
    app, w, c = start(tmp_path, first=True)
    r = up(c, plain(BUCKET)).json()
    assert c.post(f"/api/plates/{r['token']}/download", json={"loops": 3}).status_code == 200
    sp = c.post("/api/filament/spools", json={"material": "PLA", "color": "#FFFFFF", "price": 20, "net_g": 1000,
                                              "sealed": True}).json()[0]
    assert c.post("/api/filament/link", json={"printer": "p2s", "tray": 0, "spool": sp["id"]}).json()["ok"]
    return app, w, c, sp["id"], r["token"]


def report(w, state="RUNNING", layer=10, target=None):
    st = w.link.state
    st.subtask_name, st.gcode_state, st.total_layers, st.layer_num = JOB, state, 850, layer
    if target is not None:
        st.nozzle_target = target
    w.on_state(st)


def loops(app):
    return [(p["job"], p["loop_no"]) for p in app.store.production()]


def test_a_run_that_finished_while_proofloop_was_stopped_is_counted_once(tmp_path):
    app, w, c, spool, token = made(tmp_path)
    for t in (1.0, 220.0, 2.0, 3.0, 4.0, 5.0):  # loop 1, watched: FarmBoard reset, print, done, bend...
        report(w, target=t)
    assert loops(app) == [(JOB, 1)] and app.inventory.get(spool).remaining_g == pytest.approx(987.5)
    app.shutdown()

    app, w, c = start(tmp_path)  # back after loops 2 and 3 printed: the printer says the run finished
    report(w, "FINISH", 850)
    assert loops(app) == [(JOB, 1), (JOB, 2), (JOB, 3)]
    assert app.inventory.get(spool).remaining_g == pytest.approx(1000 - 3 * 12.5)
    told = [e["message"] for e in app.store.events() if e["kind"] == "loop"]
    assert any("2 loops of FL_S2_bucket ended while ProofLoop wasn't watching" in m for m in told)
    app.shutdown()

    app, w, c = start(tmp_path)  # started again, the printer still showing that run: nothing more
    c.post(f"/api/plates/{token}/download", json={"loops": 3})  # even after making the same file again
    report(w, "FINISH", 850)
    assert len(loops(app)) == 3 and app.inventory.get(spool).remaining_g == pytest.approx(962.5)
    app.shutdown()


def test_a_loop_that_ended_during_a_restart_is_counted(tmp_path):
    app, w, c, spool, _ = made(tmp_path)
    report(w, layer=10)
    w._saved_at = 0  # (layers are kept every 15 s)
    report(w, layer=840)  # loop 1 nearly done, no FarmBoard signals: a Stage 1 file
    app.shutdown()

    app, w, c = start(tmp_path)  # back while loop 2 prints its first layers
    report(w, layer=2)
    assert loops(app) == [(JOB, 1)] and w.loop_no == 2
    assert app.inventory.get(spool).remaining_g == pytest.approx(987.5)
    assert any("loop 1 ended while ProofLoop wasn't watching" in e["message"] for e in app.store.events())
    report(w, layer=3)
    report(w, layer=500)
    assert len(loops(app)) == 1  # not counted twice
    app.shutdown()


def test_a_run_proofloop_follows_from_the_middle_starts_after_the_counted_loops(tmp_path):
    app, w, c, spool, _ = made(tmp_path)
    for t in (1.0, 220.0, 2.0):
        report(w, target=t)
    app.store._db.execute("DELETE FROM progress")  # as before ProofLoop kept the run's progress
    app.shutdown()
    app, w, c = start(tmp_path)
    report(w, layer=5)
    assert w.loop_no == 2  # loop 1 is counted, so this is loop 2
    app.shutdown()


def test_sending_the_same_file_again_is_a_new_run(tmp_path):
    app, w, c, spool, _ = made(tmp_path)
    report(w, layer=10)
    report(w, "FINISH", 850)  # finished: loop 1 seen, loops 2 and 3 counted at the end
    assert len(loops(app)) == 3
    w.started()  # ProofLoop sent it again
    report(w, layer=10)
    assert w.loop_no == 1
    report(w, "FINISH", 850)
    assert loops(app)[3:] == [(JOB, 1), (JOB, 2), (JOB, 3)]  # the earlier run's loops don't count for this one
    assert app.inventory.get(spool).remaining_g == pytest.approx(1000 - 6 * 12.5)
    app.shutdown()


def test_a_file_with_spaces_in_its_name_is_followed_under_the_name_the_printer_gives_it(tmp_path):
    app, w, c = start(tmp_path, first=True)
    r = up(c, plain(BUCKET), name="Boot (red) plate 1.gcode.3mf").json()
    d = c.post(f"/api/plates/{r['token']}/download", json={"loops": 2})
    assert d.status_code == 200 and " " in d.headers["X-ProofLoop-Filename"]
    st = w.link.state
    st.subtask_name, st.gcode_state, st.total_layers, st.layer_num = "FL_S2_Boot_red_plate_1", "RUNNING", 850, 10
    for t in (1.0, 220.0, 2.0):  # the printer reports the name it was sent under: no spaces or brackets
        st.nozzle_target = t
        w.on_state(st)
    assert loops(app) == [("FL_S2_Boot_red_plate_1", 1)] and app.store.production()[0]["parts"] == 2
    assert c.get("/api/state").json()["monitor"][0]["job_loops"] == 2
    app.shutdown()


def test_files_recorded_under_their_old_names_are_renamed_once(tmp_path):
    from proofloop.monitor.store import Store
    from proofloop.monitor.tracking import job_key

    store = Store(tmp_path / "p.db")
    store.put_job("FL_S1_Plate 1", {"loops": 1})
    store.put_job("FL_S1_x (red)", {"loops": 2})
    store.put_job("FL_S1_x_red", {"loops": 3})  # newer: kept
    assert store.rename_jobs(job_key) == 2
    assert store.job("FL_S1_Plate_1") == {"loops": 1} and store.job("FL_S1_x_red") == {"loops": 3}
    assert store.job("FL_S1_Plate 1") is None and store.rename_jobs(job_key) == 0
