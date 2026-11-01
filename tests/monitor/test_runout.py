"""Spool swap timing: which loop of a run empties a tray, and what to do about it before you send.

PLATE needs 20.71 g of white PLA a loop (the slicer's figure).
"""

import pytest
from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.loopfiles import threemf
from proofloop.monitor.filament import Spool
from proofloop.monitor.runout import by_tray, forecast, run_of, tray_left
from proofloop.monitor.send import check, feeds
from proofloop.monitor.tracking import register_job
from tests.monitor.test_queue import farm, queued, running
from tests.monitor.test_send import PLATE, state


def tray(tid, typ="PLA", color="#FFFFFF", pct=None, weight=None):
    label = "ABCD"[tid // 4] + str(tid % 4 + 1)
    return {"tray": tid, "label": label, "empty": False, "type": typ, "color": color, "remain_pct": pct,
            "weight_g": weight}


EMPTY_A4 = {"tray": 3, "label": "A4", "empty": True}


def spool(grams, tid=0, printer="p2s", **kw):
    return Spool(id=f"s{tid}", name=f"White {tid}", material="PLA", color="#FFFFFF", remaining_g=grams,
                 printer=printer, tray=tid, **kw)


def loops(n, grams=100.0, tid=0):
    return [{tid: grams}] * n


# ---- what is on a tray -------------------------------------------------------------------
def test_what_is_left_comes_from_the_spool_then_the_ams_and_is_never_guessed():
    counted = tray_left(tray(0, pct=50, weight=1000), spool(412))
    assert (counted["left_g"], counted["source"], counted["sure"]) == (412, "spool", "counted")
    assert counted["says"] == "ProofLoop's count for White 0"  # the spool's count wins over the AMS
    ams = tray_left(tray(0, pct=40, weight=1000), spool(None))  # a spool with no amount: the AMS's estimate
    assert (ams["left_g"], ams["source"], ams["sure"]) == (400, "ams", "rough") and "40% of a 1000 g" in ams["says"]
    no_weight = tray_left(tray(0, pct=40), None)  # not a Bambu tag: the AMS doesn't know what the spool held
    assert no_weight["left_g"] is None and no_weight["says"] == "the AMS says 40%, but not how much the spool held"
    assert tray_left(tray(0), spool(None))["says"] == "White 0 has no amount yet: weigh it under Filament"
    assert tray_left(tray(0), None)["left_g"] is None


def test_an_unknown_amount_is_said_never_counted():
    (r,) = forecast(loops(3), [tray(0)], [], "p2s")
    assert r["status"] == "unknown" and r["left_g"] is None and not r["stops"]
    assert r["text"] == ("A1 (PLA): how much is left isn't known (no spool is linked to it and the AMS gives no "
                         "estimate); the run needs 300 g")


# ---- when a tray runs out ------------------------------------------------------------------
def test_enough_for_the_whole_run():
    (r,) = forecast(loops(3), [tray(0)], [spool(350)], "p2s")
    assert r["status"] == "enough" and r["need_g"] == 300 and r["runs_out_in"] is None
    assert r["text"] == "A1 (PLA): enough. It has 350 g (ProofLoop's count for White 0); the run needs 300 g"


def test_a_tray_that_runs_out_part_way_says_after_which_loop_to_swap():
    (r,) = forecast(loops(10), [tray(0)], [spool(350)], "p2s")
    assert (r["status"], r["runs_out_in"], r["swap_after"], r["stops"]) == ("swap", 4, 3, False)
    assert r["text"] == ("A1 (PLA) runs out during loop 4 of 10: swap the spool after loop 3. It has 350 g "
                         "(ProofLoop's count for White 0); the run needs 1000 g")
    (r,) = forecast(loops(10), [tray(0), EMPTY_A4], [spool(350)], "p2s")  # an empty tray: a second spool there
    assert "swap the spool after loop 3, or load a second spool of it in A4 for the AMS to switch to" in r["text"]
    (r,) = forecast(loops(10), [tray(0, pct=35, weight=1000)], [], "p2s")  # the AMS's rough estimate says so
    assert r["swap_after"] == 3 and "It has about 350 g (the AMS's estimate, 35% of a 1000 g spool)" in r["text"]


def test_the_ams_can_switch_to_a_tray_with_the_same_type_and_colour():
    trays = [tray(0), tray(1, pct=50, weight=1000), tray(2, color="#000000")]  # A3 is black: not a backup
    (r,) = forecast(loops(8), trays, [spool(350)], "p2s")
    assert r["status"] == "switch" and r["runs_out_in"] == 4 and r["swap_after"] is None
    assert [b["label"] for b in r["backups"]] == ["A2"]
    assert r["text"] == ("A1 (PLA) runs out during loop 4 of 8; the AMS can switch to A2 (same type and colour, "
                         "about 500 g), which covers the rest, if AMS filament backup is on in the printer's settings")
    (r,) = forecast(loops(10), trays, [spool(350)], "p2s")  # not even both together cover it
    assert (r["status"], r["swap_after"]) == ("swap", 8)
    assert r["text"].startswith("A1 and A2 (PLA) run out during loop 9 of 10: swap a spool after loop 8. "
                                "They have about 850 g")
    (r,) = forecast(loops(10), [tray(0), tray(1)], [spool(350)], "p2s")  # a backup with an unknown amount
    assert r["status"] == "swap" and r["swap_after"] == 3
    assert "A2 has the same filament, but how much is on it isn't known" in r["text"]


def test_only_a_counted_shortfall_for_the_first_loop_stops_a_send():
    (r,) = forecast(loops(3, 500), [tray(0)], [spool(350)], "p2s")
    assert r["status"] == "short" and r["stops"] and r["runs_out_in"] == 1
    assert r["text"] == ("A1 (PLA) hasn't enough for loop 1: load a new spool before it starts. It has 350 g "
                         "(ProofLoop's count for White 0); a loop needs 500 g")
    (r,) = forecast(loops(3, 500), [tray(0, pct=35, weight=1000)], [], "p2s")
    assert r["status"] == "short" and not r["stops"]  # a rough estimate warns, it never stops a send


def test_jobs_ahead_use_the_filament_first():
    # 2 loops of something else first, then this job's 3: 350 g lasts 3 of the 5 loops
    (r,) = forecast(loops(5), [tray(0)], [spool(350)], "p2s", before=2)
    assert (r["status"], r["runs_out_in"], r["swap_after"], r["need_g"]) == ("swap", 2, 1, 300)
    assert r["text"].endswith("the run needs 300 g after 200 g for the jobs ahead of it")
    (r,) = forecast(loops(7), [tray(0)], [spool(350)], "p2s", before=4)
    assert r["status"] == "before" and r["runs_out_in"] is None
    assert r["text"] == "A1 (PLA) is used up by the jobs ahead of this one on p2s: load a new spool before it starts"
    (r,) = forecast(loops(5, 120), [tray(0)], [spool(350)], "p2s", before=2)
    assert r["status"] == "short" and not r["stops"] and "loop 1 after the jobs ahead of it" in r["text"]


def test_while_printing_the_loops_are_counted_from_the_one_on_now():
    (r,) = forecast(loops(4), [tray(0)], [spool(250)], "p2s", first=7, printing=True)
    assert (r["status"], r["runs_out_in"], r["swap_after"]) == ("swap", 9, 8)
    assert r["text"].startswith("A1 (PLA) runs out during loop 9 of 10: swap the spool after loop 8")
    (r,) = forecast(loops(4), [tray(0)], [spool(50)], "p2s", first=7, printing=True)
    assert r["status"] == "short" and r["text"] == "A1 (PLA) runs out during this loop (loop 7 of 10): have a new spool ready"


def test_grams_per_slot_become_grams_per_tray_through_the_send_mapping():
    trays = state().ams.trays()  # A1 white PLA, A2 black PETG
    slots = [("1", "PLA", "#F2F2F2"), ("2", "PETG", "#000000"), ("3", "ABS", "#FF0000")]
    fed = feeds(slots, trays)
    assert fed == {"1": 0, "2": 1}  # ABS isn't loaded: the send's check says so, nothing is counted for it
    assert by_tray([{"1": 10, "2": 5, "3": 7}] * 2, fed) == [{0: 10, 1: 5}] * 2
    assert feeds([("1", "PLA", "#F2F2F2")], []) == {"1": 254}  # no AMS: the external spool
    job = {"plates": [{"slots": [["1", "PLA", "#FFF"]], "grams": {"1": 10}},
                      {"slots": [["1", "PLA", "#FFF"], ["2", "PETG", "#000"]], "grams": {"1": 1, "2": 4}}],
           "order": [0, 0, 1, 1]}
    assert run_of(job) == ([("1", "PLA", "#FFF"), ("2", "PETG", "#000")],
                           [{"1": 10}, {"1": 10}, {"1": 1, "2": 4}, {"1": 1, "2": 4}])
    assert run_of(job, 3)[1] == [{"1": 1, "2": 4}] * 2


# ---- before a send -----------------------------------------------------------------------
def test_the_send_check_notes_a_swap_and_stops_only_when_loop_one_cant_be_done():
    s = state()
    slots = [("1", "PLA", "#F2F2F2")]
    p, reasons = check("p2s", s, "N7", "N7", slots, [], run=[{"1": 100}] * 10, spools=[spool(350)])
    assert not reasons and p.notes == [p.filament[0]["text"]] and p.filament[0]["swap_after"] == 3
    p, reasons = check("p2s", s, "N7", "N7", slots, [], run=[{"1": 100}] * 3, spools=[spool(350)])
    assert not reasons and p.notes == [] and p.filament[0]["status"] == "enough"
    p, reasons = check("p2s", s, "N7", "N7", slots, [], run=[{"1": 500}], spools=[spool(350)])
    assert reasons == [p.filament[0]["text"]] and "hasn't enough for loop 1" in reasons[0]
    p, reasons = check("p2s", s, "N7", "N7", slots, [], run=[{"1": 500}], spools=[spool(350)], hold=False)
    assert not reasons and "hasn't enough for loop 1" in p.notes[0]  # hold when short is off: only a note
    p, reasons = check("p2s", s, "N7", "N7", slots, [], run=[{"1": 100}] * 3, spools=[])
    assert not reasons and p.notes == [] and p.filament[0]["status"] == "unknown"


@pytest.fixture()
def app(tmp_path):
    a = farm(tmp_path, "p2s")
    a.settings.send_to_printer = True
    yield a
    a.shutdown()


def link(app, grams, tid=0):
    sp = app.inventory.save(Spool(material="PLA", color="#F0F0F0", name="White", remaining_g=grams))
    app.tracker.link("p2s", tid, sp.id)
    return sp


def test_the_send_panel_counts_the_loops_you_set(app):
    c = TestClient(create_app(app))
    token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
    link(app, 70)  # 3 loops of 20.71 g, not 4
    (t,) = c.post(f"/api/plates/{token}/printers", json={"loops": 3}).json()["printers"]
    assert t["ready"] and t["notes"] == [] and t["filament"][0]["status"] == "enough"
    (t,) = c.post(f"/api/plates/{token}/printers", json={"loops": 6}).json()["printers"]
    assert t["ready"] and t["filament"][0]["swap_after"] == 3
    assert t["notes"] == ["A1 (PLA) runs out during loop 4 of 6: swap the spool after loop 3. It has 70 g "
                          "(ProofLoop's count for White); the run needs 124 g"]
    # Make a loop file's plate panel says the same for the printer you pick
    s = c.post(f"/api/plates/{token}/filament", json={"loops": 6, "printer": "p2s"}).json()["slots"][0]
    assert s["ams"]["swap_after"] == 3 and s["ams"]["label"] == "A1"
    assert c.post(f"/api/plates/{token}/filament", json={"loops": 6}).json()["slots"][0]["ams"] is None


def test_a_send_known_to_run_out_in_loop_one_is_refused(app):
    c = TestClient(create_app(app))
    token = c.post("/api/plates", files={"file": (PLATE.name, PLATE.read_bytes())}).json()["token"]
    link(app, 10)
    (t,) = c.post(f"/api/plates/{token}/printers", json={"loops": 2}).json()["printers"]
    assert not t["ready"] and "A1 (PLA) hasn't enough for loop 1" in t["reasons"][0]
    r = c.post(f"/api/plates/{token}/send", json={"printer": "p2s", "confirm": "p2s", "loops": 2})
    assert r.status_code == 409 and "hasn't enough for loop 1" in r.json()["detail"]
    assert not app.watches["p2s"].link.sent
    app.settings.filament.hold_when_short = False  # you rely on spools ProofLoop doesn't know about
    (t,) = c.post(f"/api/plates/{token}/printers", json={"loops": 2}).json()["printers"]
    assert t["ready"] and "hasn't enough for loop 1" in t["notes"][0]


def test_queued_jobs_say_whether_the_spool_lasts_after_the_jobs_ahead(app):
    c = TestClient(create_app(app))
    link(app, 100)  # 4 loops' worth
    queued(app, "p2s", "p2s", loops=3)
    first, second = c.get("/api/queue").json()[0]["jobs"]
    assert first["filament"][0]["status"] == "enough"
    assert (second["filament"][0]["status"], second["filament"][0]["swap_after"]) == ("swap", 1)
    assert second["filament"][0]["text"].startswith("A1 (PLA) runs out during loop 2 of 3: swap the spool after loop 1")
    app.watches["p2s"].link.state.online = False  # its AMS can't be seen: nothing is said
    assert "filament" not in c.get("/api/queue").json()[0]["jobs"][0]


def test_the_printer_card_says_when_a_spool_swap_is_due(app):
    c =TestClient(create_app(app))
    link(app, 100)
    register_job(app.store, "FL_S2_bracket.gcode.3mf", [(threemf.load(PLATE), 10, 2)], "p2s")
    (card,) = c.get("/api/state").json()["monitor"]
    assert card["spool_swap"] is None  # nothing printing
    running(app, "p2s", "FL_S2_bracket", loop=3)
    (card,) = c.get("/api/state").json()["monitor"]
    # loops 3..7 need 103.6 g: the fifth of them, loop 7, runs out
    assert card["spool_swap"]["after_loop"] == 6 and card["spool_swap"]["runs_out_in"] == 7
    assert card["spool_swap"]["text"] == "Spool swap due after loop 6: A1 (PLA) runs out during loop 7"
    app.inventory.set_remaining(app.inventory.all()[0].id, 15)
    (card,) = c.get("/api/state").json()["monitor"]
    assert card["spool_swap"]["text"] == "A1 (PLA) runs out during this loop (loop 3 of 10): have a new spool ready"
    # a queued job behind the run that prints now counts what that run still needs
    queued(app, "p2s", loops=1)
    app.inventory.set_remaining(app.inventory.all()[0].id, 1000)
    (job,) = c.get("/api/queue").json()[0]["jobs"]
    assert job["filament"][0]["status"] == "enough" and "after 166 g for the jobs ahead of it" in job["filament"][0]["text"]
