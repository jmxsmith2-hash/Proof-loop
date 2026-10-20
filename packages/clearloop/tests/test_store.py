from clearloop.store import Store


def test_next_job_respects_pinning_models_and_loaded_material():
    s = Store()
    a = s.add_job("asa part", "/a", materials=["ASA"])
    b = s.add_job("x1 only", "/b", models=["x1c"], materials=["PLA"])
    c = s.add_job("pinned", "/c", printer="other", materials=["PLA"])
    d = s.add_job("pla", "/d", materials=["PLA"])
    assert s.next_job("p1", "p1s", ["PLA Basic", "PETG"])["id"] == d
    assert s.next_job("p1", "p1s", ["ASA"])["id"] == a
    assert s.next_job("p1", "p1s", None)["id"] == a  # no AMS info: do not filter on material
    assert s.next_job("x1", "x1c", ["PLA"])["id"] == b
    assert s.next_job("other", "p1s", ["PLA"])["id"] == c
    assert s.claim_job(d, "p1") and not s.claim_job(d, "p2")


def test_priority_wins():
    s = Store()
    s.add_job("low", "/a")
    hi = s.add_job("high", "/b", priority=5)
    assert s.next_job("p1", "p1s", None)["id"] == hi


def test_stats_and_plate_wear():
    s = Store()
    for result in ("cleared", "cleared", "recovered", "blocked"):
        lid = s.start_loop("p1", None)
        s.update_loop(lid, finished=9e9, result=result, trigger_seen=1)
        s.plate_cycle("p1", retried=result != "cleared")
    row = s.stats()["printers"][0]
    assert row["loops"] == 4 and row["success_rate"] == 0.75 and row["first_try_rate"] == 0.5
    plate = s.active_plate("p1")
    assert plate["cycles"] == 4 and plate["retries"] == 2
    new = s.replace_plate("p1", "cryogrip", 800)
    assert new["cycles"] == 0 and new["type"] == "cryogrip"
