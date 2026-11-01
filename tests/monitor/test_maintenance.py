"""Maintenance: counters from the recorded prints, due and overdue per unit, Done, and the one notice."""

import time

import pytest

from proofloop.monitor import maintenance as maint
from proofloop.monitor.maintenance import DAY, HOUR, state_of
from proofloop.monitor.summary import summary
from tests.monitor.test_queue import farm

NOW = time.time()


def later():  # a moment from now, so rows stamped with the real clock are counted however slow the run is
    return time.time() + 5


def print_ended(app, printer, hours_ago, hours=1.0, outcome="finished"):
    ended = NOW - hours_ago * HOUR
    app.store.add_outcome({"printer": printer, "job": "FL_S2_x", "outcome": outcome, "started": ended - hours * HOUR,
                           "ended": ended})


def start_counting(app, printer, days_ago, *tasks):
    for t in tasks or maint.BUILTIN_IDS:
        app.store.maintenance_start(printer, t, NOW - days_ago * DAY)


def test_counters_come_from_the_recorded_prints(tmp_path):
    app = farm(tmp_path, "a", "b")
    start_counting(app, "a", 10)
    for i in range(30):
        print_ended(app, "a", hours_ago=3 + 3 * i, hours=1.0)
    print_ended(app, "a", hours_ago=2, hours=2.5, outcome="failed")        # a failed print ran, so it counts
    print_ended(app, "a", hours_ago=20 * 24, hours=5)                        # before it started counting
    print_ended(app, "b", hours_ago=1)                                       # another printer
    app.store.add_production("a", "FL_S2_x", 1, 4, 20.0)
    app.store.add_production("a", "FL_S2_x", 2, 4, 9.0, ok=False)            # a failed loop is not a loop made
    app.store.add_usage(grams=12.5, kind="loop", printer="a", ts=NOW - 100)
    app.store.add_usage(grams=100, kind="weigh", printer="a", ts=NOW - 100)  # a weigh-in is not use
    app.store.add_usage(grams=7, kind="loop", printer="b", ts=NOW - 100)

    items = {i["task"]: i for i in maint.printer_status(app, "a", later())}
    counted = items["plate"]["counted"]
    assert counted["prints"] == 31 and counted["hours"] == 32.5
    assert counted["loops"] == 1 and counted["grams"] == 12 and 10 <= counted["days"] <= 10.1
    assert items["plate"]["used"] == 31 and items["plate"]["unit"] == "prints"
    assert items["nozzle"]["used"] == 32.5 and items["nozzle"]["unit"] == "hours"
    assert items["carbon"]["unit"] == "days" and items["carbon"]["used"] == 10.0
    assert maint.printer_status(app, "b", later())[0]["counted"]["prints"] == 0  # b is seen for the first time now
    app.shutdown()


def test_a_print_with_no_sensible_length_adds_no_hours(tmp_path):
    app = farm(tmp_path, "a")
    start_counting(app, "a", 1)
    app.store.add_outcome({"printer": "a", "outcome": "unknown", "started": NOW - 200 * HOUR, "ended": NOW - 60})
    app.store.add_outcome({"printer": "a", "outcome": "finished"})  # no times at all
    c = maint.printer_status(app, "a", later())[0]["counted"]
    assert (c["prints"], c["hours"]) == (2, 0)
    app.shutdown()


@pytest.mark.parametrize("used,state", [(0, "ok"), (19, "ok"), (20, "soon"), (24, "soon"), (25, "due"),
                                        (31, "due"), (32, "overdue"), (100, "overdue")])
def test_due_and_overdue_are_shares_of_the_interval(used, state):
    assert state_of(used, 25) == state


def test_each_unit_is_compared_with_its_own_counter(tmp_path):
    app = farm(tmp_path, "a")
    cfg = app.settings.maintenance
    cfg.custom = [maint.CustomTask(id="h", name="Hours", every=10, unit="hours"),
                  maint.CustomTask(id="l", name="Loops", every=2, unit="loops"),
                  maint.CustomTask(id="g", name="Grams", every=50, unit="grams"),
                  maint.CustomTask(id="p", name="Prints", every=3, unit="prints"),
                  maint.CustomTask(id="d", name="Days", every=5, unit="days")]
    start_counting(app, "a", 6, "h", "l", "g", "p", "d")
    for i in range(3):
        print_ended(app, "a", hours_ago=i + 1, hours=2)           # 3 prints, 6 hours
    app.store.add_production("a", "x", 1, 1, 1.0)
    app.store.add_production("a", "x", 2, 1, 1.0)
    app.store.add_usage(grams=30, kind="partial", printer="a", ts=NOW - 5)
    s = {i["task"]: (i["state"], i["pct"]) for i in maint.printer_status(app, "a", later())}
    assert s["h"] == ("ok", 60) and s["l"] == ("due", 100) and s["g"] == ("ok", 60)
    assert s["p"] == ("due", 100) and s["d"] == ("due", 120)
    app.shutdown()


def test_edited_intervals_and_switched_off_tasks(tmp_path):
    app = farm(tmp_path, "a")
    app.settings.maintenance.edits = {"plate": maint.TaskEdit(every=5), "belts": maint.TaskEdit(enabled=False)}
    listed = {t["id"]: t for t in maint.tasks(app.settings.maintenance)}
    assert listed["plate"]["every"] == 5 and listed["plate"]["default"]["every"] == 25
    assert listed["belts"]["enabled"] is False
    names = {i["task"] for i in maint.printer_status(app, "a", NOW)}
    assert "belts" not in names and "plate" in names
    # only the two intervals Bambu's wiki gives are marked as cited
    assert {t["id"] for t in maint.tasks(maint.MaintenanceSettings()) if t["source"]} == {"carbon", "screws"}
    app.shutdown()


def test_done_starts_the_count_again_and_keeps_what_was_used(tmp_path):
    app = farm(tmp_path, "a")
    start_counting(app, "a", 10, "plate")
    for i in range(35):
        print_ended(app, "a", hours_ago=i + 1)
    assert maint.printer_status(app, "a", later())[0]["state"] == "overdue"

    item = maint.mark_done(app, "a", "plate", "  new PEI plate ", now=NOW + 60)
    assert item["state"] == "ok" and item["used"] == 0 and item["note"] == "new PEI plate"
    assert item["last_done"] == NOW + 60 and item["counted"]["hours"] == 0
    (log,) = app.store.maintenance_log("a")
    assert log["task"] == "plate" and log["used"]["prints"] == 35 and log["used"]["hours"] == 35.0

    app.store.add_outcome({"printer": "a", "outcome": "finished", "started": NOW + 100, "ended": NOW + 200})
    assert maint.printer_status(app, "a", NOW + 300)[0]["used"] == 1  # counting from the Done
    app.shutdown()


def test_done_on_an_earlier_day_counts_what_was_printed_since(tmp_path):
    app = farm(tmp_path, "a")
    three_days_ago = time.strftime("%Y-%m-%d", time.localtime(NOW - 3 * DAY))
    for days in (1, 2, 4):
        print_ended(app, "a", hours_ago=24 * days)
    item = maint.mark_done(app, "a", "plate", on=three_days_ago, now=NOW)
    assert item["used"] == 2 and item["state"] == "ok"
    with pytest.raises(ValueError):
        maint.mark_done(app, "a", "plate", on=time.strftime("%Y-%m-%d", time.localtime(NOW + 2 * DAY)), now=NOW)
    with pytest.raises(ValueError):
        maint.mark_done(app, "a", "plate", on="last week", now=NOW)
    with pytest.raises(KeyError):
        maint.mark_done(app, "nope", "plate", now=NOW)
    with pytest.raises(KeyError):
        maint.mark_done(app, "a", "nope", now=NOW)
    app.shutdown()


def due_plate(app, printer="a", prints=26):
    start_counting(app, printer, 10, *maint.BUILTIN_IDS)
    for i in range(prints):
        print_ended(app, printer, hours_ago=i + 1, hours=0.5)


def test_the_notice_goes_out_once_until_done_or_a_week(tmp_path):
    app = farm(tmp_path, "a")
    everything = []
    app.notifier.send = lambda event, image=None, wait=False, every=False: everything.append(event)

    def sent():  # the tasks counted in days come due as the days pass: follow the plate's notices
        return [e for e in everything if "build plate" in e["message"]]

    due_plate(app)
    app.tell_maintenance(NOW)
    (e,) = sent()
    assert e["level"] == "warning" and e["kind"] == "maintenance_due" and e["printer"] == "a"
    assert "26 prints" in e["message"] and "every 25 prints" in e["message"]
    assert [x["kind"] for x in app.store.events()] == ["maintenance_due"]

    app.tell_maintenance(NOW + 600)
    app.tell_maintenance(NOW + 6 * DAY)
    assert len(sent()) == 1                                 # not again within the week
    app.tell_maintenance(NOW + 8 * DAY)
    assert len(sent()) == 2                                 # still not done: a week later, once more

    maint.mark_done(app, "a", "plate", now=NOW + 9 * DAY)
    app.tell_maintenance(NOW + 20 * DAY)
    assert len(sent()) == 2                                 # done: nothing
    for i in range(26):                                     # due again after the next 26 prints: told again
        app.store.add_outcome({"printer": "a", "outcome": "finished", "started": NOW + 9.5 * DAY + i,
                               "ended": NOW + 9.6 * DAY + i})
    app.tell_maintenance(NOW + 21 * DAY)
    assert len(sent()) == 3
    app.shutdown()


def test_one_notice_for_each_task_that_comes_due(tmp_path):
    app = farm(tmp_path, "a")
    sent = []
    app.notifier.send = lambda event, image=None, wait=False, every=False: sent.append(event)
    due_plate(app, prints=51)  # the plate (25 prints) and the purge chute (50) are both due; nothing else is
    app.tell_maintenance(NOW)
    assert len(sent) == 2
    assert {"Clean the build plate", "Empty the purge chute"} == {
        n for n in ("Clean the build plate", "Empty the purge chute") if any(n in e["message"] for e in sent)}
    app.shutdown()


def test_no_notice_when_reminders_are_off_and_none_is_held_over(tmp_path):
    app = farm(tmp_path, "a")
    sent = []
    app.notifier.send = lambda event, image=None, wait=False, every=False: sent.append(event)
    due_plate(app)
    app.settings.maintenance.remind = False
    app.tell_maintenance(NOW)
    assert sent == []
    app.settings.maintenance.remind = True
    app.tell_maintenance(NOW + 1)
    assert len(sent) == 1                                   # turning it back on tells you then
    app.settings.maintenance.edits["plate"] = maint.TaskEdit(every=100)   # no longer due: the mark clears
    app.tell_maintenance(NOW + 2)
    app.settings.maintenance.edits["plate"] = maint.TaskEdit(every=10)
    app.tell_maintenance(NOW + 3)
    assert len(sent) == 2
    app.shutdown()


def test_maintenance_never_touches_a_running_print(tmp_path):
    app = farm(tmp_path, "a")
    due_plate(app, prints=500)
    app.tell_maintenance(NOW)
    before = (app.watches["a"].waiting_for_you, app.watches["a"].status)
    app.tell_maintenance(NOW + 1)
    assert (app.watches["a"].waiting_for_you, app.watches["a"].status) == before == (False, "waiting for printer")
    app.shutdown()


def test_the_daily_summary_lists_what_is_due(tmp_path):
    quiet = farm(tmp_path / "quiet", "b")
    s = summary(quiet)
    assert s["maintenance"] == [] and "Maintenance" not in s["text"]
    quiet.shutdown()
    app = farm(tmp_path, "a", "b")
    due_plate(app, "a", prints=51)
    s = summary(app)
    assert {(m["printer"], m["task"]) for m in s["maintenance"]} == {("a", "plate"), ("a", "purge")}
    assert "Maintenance due: a: clean the build plate; a: empty the purge chute." in s["text"]
    app.shutdown()


def test_tasks_and_what_was_done_survive_a_backup(tmp_path):
    from proofloop.app import backup

    app = farm(tmp_path / "one", "a")
    app.settings.maintenance.edits["plate"] = maint.TaskEdit(every=40)
    app.settings.maintenance.custom = [maint.CustomTask(id="c1", name="Wipe the window", every=3, unit="days")]
    maint.mark_done(app, "a", "plate", "before", now=NOW)
    raw = backup.make(app)
    app.shutdown()

    other = farm(tmp_path / "two", "x")
    backup.restore(other, raw)
    assert other.settings.maintenance.edits["plate"].every == 40 and other.settings.maintenance.custom[0].id == "c1"
    (row,) = other.store.maintenance_log("a")
    assert row["note"] == "before" and other.store.maintenance("a")["plate"]["done"] == NOW
    other.shutdown()


def test_settings_keep_your_changes(tmp_path):
    from proofloop.core.settings import SettingsStore

    app = farm(tmp_path, "a")
    app.settings.maintenance.edits["screws"] = maint.TaskEdit(every=60, unit="days")
    app.settings.maintenance.remind = False
    app.save_settings()
    again = SettingsStore(tmp_path).load().maintenance
    assert again.edits["screws"].every == 60 and again.remind is False and again.custom == []
    app.shutdown()
