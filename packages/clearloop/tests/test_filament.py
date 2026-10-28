import json

import pytest
from fastapi.testclient import TestClient
from fixtures import BUCKET, SMALL, plain
from test_server import FakeLink, up

from clearloop import threemf
from clearloop.filament import FilamentSettings, Inventory, Spool, requirements, resolve
from clearloop.gcode.usage import layer_usage, used_by_layer
from clearloop.printer.ams import AmsState, global_id, label
from clearloop.server import create_app
from clearloop.service import App
from clearloop.spoolman import to_spool
from clearloop.store import Store

SLOT = [("1", "PLA", "#FFFFFF")]


def inv(**settings):
    events = []
    i = Inventory(Store(), lambda: FilamentSettings(**settings), lambda *a, **k: events.append(a))
    return i, events


def tray(tid, typ="PLA", color="FFFFFFFF", **kw):
    return {"id": str(tid), "tray_type": typ, "tray_color": color, "remain": kw.pop("remain", -1), **kw}


# ---- the file ---------------------------------------------------------------------------
def test_presets_and_layer_usage_come_from_the_file():
    sf = threemf.loads(plain(SMALL))
    p = sf.info.presets["1"]
    assert p["preset"] == "Bambu PLA Basic @BBL P2S" and p["density"] == 1.26 and p["diameter"] == 1.75
    assert p["slicer_cost_per_kg"] == 24.99 and p["ams_drying_temp"] == 45 and p["ams_drying_hours"] == 12
    u = layer_usage(sf.gcode, ["1"], {"1": 1.75}, {"1": 1.26})
    g = u["grams"]["1"]
    assert u["layers"] == 60 and g == sorted(g)  # 12 mm at 0.2 mm, never decreasing
    mm_per_gram = 1000 / (3.14159265 * 0.875**2 * 1.26)
    assert g[-1] * mm_per_gram == pytest.approx(8 + 60 * 4 + 20 * 4, rel=1e-3)  # purge + both parts' walls
    assert used_by_layer(u, 0) == {"1": 0.0} and used_by_layer(u, 999)["1"] == g[-1]
    assert layer_usage(sf.gcode, ["1"], {}, {}) is None  # no density -> never estimated


def test_tool_changes_move_usage_to_the_next_slot():
    g = "; EXECUTABLE_BLOCK_START\nM83\nM73 L1\nG1 X1 E10\nT1\nG1 X2 E5\nM73 L2\nG1 X3 E2\nT0\nG1 X1 E1\n; EXECUTABLE_BLOCK_END"
    u = layer_usage(g, ["1", "2"], {"1": 1.75, "2": 1.75}, {"1": 1.0, "2": 1.0})
    area = 3.14159265 * 0.875**2 / 1000
    assert u["grams"]["1"] == [pytest.approx(10 * area, abs=1e-3), pytest.approx(11 * area, abs=1e-3)]
    assert u["grams"]["2"][-1] == pytest.approx(7 * area, abs=1e-3)


# ---- the AMS --------------------------------------------------------------------------------
def test_ams_reports_merge_like_the_printer_sends_them():
    a = AmsState()
    a.merge_print(
        {
            "ams": {
                "tray_now": "1",
                "ams": [
                    {
                        "id": "0",
                        "humidity_raw": "23",
                        "temp": "28.5",
                        "tray": [
                            tray(0, remain=80, tray_uuid="A" * 32, tray_sub_brands="PLA Basic"),
                            tray(1, "PETG", "000000FF"),
                        ],
                    }
                ],
            }
        }
    )
    # a later partial report: zero tags mean "not read", not "changed"; an empty type means slot emptied
    a.merge_print(
        {
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {"id": "0", "remain": 75, "tray_uuid": "0" * 32},
                            {"id": "1", "tray_type": ""},
                        ],
                    }
                ]
            }
        }
    )
    a.merge_print({"vt_tray": {"id": "254", "tray_type": "TPU", "tray_color": "FF0000FF"}})
    t = {x["tray"]: x for x in a.trays()}
    assert t[0]["tag"] == "A" * 32 and t[0]["remain_pct"] == 75 and t[0]["color"] == "#FFFFFF"
    assert t[0]["humidity_pct"] == 23 and t[0]["sub_brand"] == "PLA Basic"
    assert t[1]["empty"] and t[254]["type"] == "TPU" and t[254]["label"] == "Ext"
    assert a.tray_now == 1 and a.units_dict()[0]["name"] == "AMS A"
    assert [global_id(1, 2), global_id(128, 0), label(6), label(129)] == [6, 128, "B3", "HT-B"]


def test_unknown_remain_is_never_shown_as_a_number():
    a = AmsState()
    a.merge_print({"ams": {"ams": [{"id": "0", "tray": [tray(0, remain=-1), tray(1, remain=140)]}]}})
    assert [x["remain_pct"] for x in a.trays()] == [None, None]


# ---- the inventory --------------------------------------------------------------------------
def test_usage_weighing_and_undo():
    i, events = inv(low_g=100)
    sp = i.save(Spool(material="pla", color="ffffff", price=20, net_g=1000), sealed=True)
    assert sp.material == "PLA" and sp.color == "#FFFFFF" and sp.remaining_g == 1000 and sp.per_kg == 20
    assert i.save(Spool(material="PLA", net_g=1000)).remaining_g is None  # opened spool: weigh it
    uid = i.use(sp.id, 950, "loop", material="PLA")
    assert i.get(sp.id).remaining_g == 50 and events[-1][1] == "filament_low"
    i.undo(uid)
    assert i.get(sp.id).remaining_g == 1000
    with pytest.raises(ValueError, match="empty spool"):
        i.weigh(sp.id, 600)
    s = i.get(sp.id)
    s.tare_g = 250
    i.save(s)
    assert i.weigh(sp.id, 600).remaining_g == 350
    assert i.store.usage(0)[0]["kind"] == "weigh"
    loose = i.use(None, 12.5, "loop", material="PLA", color="#FFFFFF")
    assert i.summary()["unmatched"] == 1
    i.assign_usage(loose, sp.id)
    assert i.get(sp.id).remaining_g == 337.5 and i.summary()["unmatched"] == 0
    assert i.summary()["used_g"]["1"] == 12.5 and i.summary()["used_cost"]["1"] == 0.25


def test_which_spool_feeds_a_slot():
    i, _ = inv()
    a = i.save(Spool(material="PLA", color="#FFFFFF", net_g=1000, remaining_g=400))
    b = i.save(Spool(material="PLA", color="#FFFFFF", net_g=1000, remaining_g=300))
    trays = AmsState()
    trays.merge_print({"ams": {"ams": [{"id": "0", "tray": [tray(0), tray(1), tray(2, "PETG")]}]}})
    t = trays.trays()
    pick = resolve(i, "p2s", SLOT, t)["1"]
    assert pick["spool"] is None and pick["trays"] == [0, 1]  # two trays, two spools: nobody guesses
    i.save(a.model_copy(update={"printer": "p2s", "tray": 0}))
    pick = resolve(i, "p2s", SLOT, t, tray_hint=0)["1"]
    assert pick["spool"] == a.id and "fed from" in pick["how"]
    i.store.assign("", "PLA|#FFFFFF", b.id)
    assert resolve(i, "p2s", SLOT, [])["1"]["spool"] == b.id
    # the AMS switches to the second tray when the first runs out, so both count
    i.save(i.get(b.id).model_copy(update={"printer": "p2s", "tray": 1}))
    req = requirements(i, "p2s", SLOT, {"1": 100}, 10, t)
    assert req["slots"][0]["available_g"] == 700 and req["max_loops"] == 7 and req["short"]


# ---- the whole loop: make a file, print it, charge the spool -----------------------------
def app_client(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    from clearloop.config import PrinterConfig

    app.add_printer(PrinterConfig(name="p2s", host="1.2.3.4", serial="S", access_code="x", model_id="N7"))
    return TestClient(create_app(app)), app


def test_loops_are_charged_to_the_spool_that_fed_them(tmp_path):
    c, app = app_client(tmp_path)
    r = up(c, plain(BUCKET)).json()
    d = c.post(f"/api/plates/{r['token']}/download", json={"loops": 3})
    assert d.status_code == 200
    job = app.store.job("FL_S2_bucket")
    assert job["loops"] == 3 and job["plates"][0]["grams"] == {"1": 12.5} and job["plates"][0]["parts"] == 2

    sp = c.post(
        "/api/filament/spools",
        json={"material": "PLA", "color": "#FFFFFF", "price": 20, "net_g": 1000, "sealed": True},
    ).json()[0]
    w = app.watches["p2s"]
    st = w.link.state
    st.ams.merge_print({"ams": {"tray_now": "0", "ams": [{"id": "0", "tray": [tray(0)]}]}})
    assert c.post("/api/filament/link", json={"printer": "p2s", "tray": 0, "spool": sp["id"]}).json()["ok"]
    need = c.post(f"/api/plates/{r['token']}/filament", json={"loops": 100, "printer": "p2s"}).json()
    assert (
        need["max_loops"] == 80 and need["short"] and need["slots"][0]["preset"]["preset"].startswith("Bambu")
    )

    st.subtask_name, st.gcode_state, st.total_layers, st.layer_num = "FL_S2_bucket", "RUNNING", 850, 10
    for t in (1.0, 220.0, 2.0, 3.0, 4.0, 5.0):  # FarmBoard: reset, printing, done, bend, safety, door
        st.nozzle_target = t
        w.on_state(st)
    assert app.inventory.get(sp["id"]).remaining_g == pytest.approx(987.5)
    assert w.as_dict()["loop_no"] == 1
    prod = app.store.production()
    assert prod[-1]["parts"] == 2 and prod[-1]["grams"] == 12.5

    # loop 2 is cancelled at layer 101: charge what its first 100 layers used, from the G-code
    st.nozzle_target = 1.0
    w.on_state(st)
    st.layer_num = 101
    w.on_state(st)
    st.gcode_state = "FAILED"
    w.on_state(st)
    plate = app.store.job("FL_S2_bucket")["plates"][0]
    partial = plate["layers"]["grams"]["1"][99]
    assert app.inventory.get(sp["id"]).remaining_g == pytest.approx(987.5 - partial, abs=1e-3)
    row = app.store.usage(0)[0]
    assert row["kind"] == "partial" and "layer 100 of" in row["note"]

    # a spool that can't cover the next loop holds the loop-end pause
    c.post(f"/api/filament/spools/{sp['id']}/remaining", json={"grams": 5})
    st.gcode_state, st.nozzle_target = "RUNNING", 1.0
    w.on_state(st)
    msg = app.tracker.blocker(w, st)
    assert "5 g left" in msg and "12.5 g" in msg
    app.settings.filament.hold_when_short = False
    assert app.tracker.blocker(w, st) is None


def test_rfid_spools_link_themselves_and_unlink_when_taken_out(tmp_path):
    c, app = app_client(tmp_path)
    sp = app.inventory.save(Spool(material="PLA", color="#FFFFFF", tag="B" * 32))
    w = app.watches["p2s"]
    st = w.link.state
    st.ams.merge_print(
        {
            "ams": {
                "ams": [{"id": "0", "tray": [tray(2, tray_uuid="B" * 32, drying_temp="55", drying_time="8")]}]
            }
        }
    )
    w.on_state(st)
    got = app.inventory.get(sp.id)
    assert (got.printer, got.tray, got.drying_temp, got.drying_hours) == ("p2s", 2, 55, 8)
    st.ams.merge_print({"ams": {"ams": [{"id": "0", "tray": [{"id": "2", "tray_type": ""}]}]}})
    w.on_state(st)
    assert app.inventory.get(sp.id).tray is None


def test_unknown_files_are_never_charged(tmp_path):
    c, app = app_client(tmp_path)
    sp = app.inventory.save(Spool(material="PLA", color="#FFFFFF", net_g=1000), sealed=True)
    w = app.watches["p2s"]
    st = w.link.state
    st.subtask_name, st.gcode_state = "FL_S2_someone-elses", "RUNNING"
    for t in (1.0, 2.0):
        st.nozzle_target = t
        w.on_state(st)
    assert app.inventory.get(sp.id).remaining_g == 1000
    assert any("isn't counted" in e["message"] for e in app.store.events())


def test_costs_use_inventory_prices_and_old_price_lists_move_over(tmp_path):
    from clearloop.config import SettingsStore

    s = SettingsStore(tmp_path)
    st = s.load()
    from clearloop.costing import Spool as PriceListSpool

    st.costs.spools = [PriceListSpool(name="old", type="PLA", color="#FFFFFF", price=30, weight_g=1000)]
    s.save(st)
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    spools = app.inventory.all()
    assert [(x.name, x.price, x.net_g, x.remaining_g) for x in spools] == [("old", 30, 1000, None)]
    assert not app.settings.costs.spools
    c = TestClient(create_app(app))
    r = up(c, plain(BUCKET)).json()
    rep = c.post(f"/api/plates/{r['token']}/cost", json={"loops": 1}).json()
    assert rep["materials"][0]["cost"] == pytest.approx(12.5 / 1000 * 30)
    assert rep["materials"][0]["spool"] == spools[0].id


def test_spoolman_fields_map_without_inventing_weights(tmp_path, monkeypatch):
    sm = {
        "id": 7,
        "remaining_weight": 640.5,
        "spool_weight": None,
        "initial_weight": None,
        "price": None,
        "extra": {"tag": json.dumps("C" * 32)},
        "filament": {
            "name": "PLA Basic",
            "material": "PLA",
            "color_hex": "a6a9aa",
            "weight": 1000,
            "price": 19.99,
            "density": 1.24,
            "vendor": {"name": "Bambu Lab"},
        },
    }
    d = to_spool(sm)
    assert d["tare_g"] is None and d["net_g"] == 1000 and d["price"] == 19.99 and d["tag"] == "C" * 32
    c, app = app_client(tmp_path)
    c.put("/api/filament/settings", json={"spoolman_url": "http://spoolman.local:7912"})
    from clearloop import spoolman

    monkeypatch.setattr(spoolman.Spoolman, "spools", lambda self: [sm])
    assert c.post("/api/filament/spoolman/import").json() == {"added": 1, "updated": 0}
    assert c.post("/api/filament/spoolman/import").json() == {"added": 0, "updated": 1}
    sp = app.inventory.all()[0]
    assert sp.spoolman_id == 7 and sp.color == "#A6A9AA" and sp.remaining_g == 640.5
    assert c.put("/api/filament/settings", json={"spoolman_url": "spoolman.local"}).status_code == 422


def test_filament_api_export_and_backup_restore(tmp_path):
    c, app = app_client(tmp_path)
    made = c.post(
        "/api/filament/spools",
        json={"material": "PETG", "color": "#000000", "net_g": 1000, "sealed": True, "copies": 3},
    ).json()
    assert len(made) == 3
    sid = made[0]["id"]
    c.post(f"/api/filament/spools/{sid}/use", json={"grams": 40, "note": "test print"})
    c.post(f"/api/filament/spools/{sid}/dried")
    f = c.get("/api/filament").json()
    one = next(s for s in f["spools"] if s["id"] == sid)
    assert one["remaining_g"] == 960 and one["dried"] and one["remaining_pct"] == 96.0
    assert f["summary"]["spools"] == 3 and f["summary"]["used_g"]["7"] == 40
    assert "PETG" in c.get("/api/filament/export.csv").text
    assert "test print" in c.get("/api/filament/export.csv?what=usage").text
    usage = c.get("/api/filament/usage?days=7").json()
    assert usage["daily"][-1]["by_material"] == {"PETG": 40}
    backup = c.get("/api/backup").content
    c.delete(f"/api/filament/spools/{sid}")
    assert len(app.inventory.all()) == 2
    r = c.post("/api/restore", files={"file": ("b.json", backup)}).json()
    assert r["spools"] == 3 and r["printers"] == 1 and "p2s" in app.watches
    assert app.inventory.get(sid).remaining_g == 960
    assert c.post("/api/restore", files={"file": ("b.json", b"{}")}).status_code == 422
    ov = c.get("/api/overview").json()
    assert ov["runs"][0]["printer"] == "p2s" and ov["filament"]["spools"] == 3
