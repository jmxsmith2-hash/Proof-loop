"""The Maintenance routes and page."""

import time

from fastapi.testclient import TestClient

from proofloop.app.loop_server import create_app
from proofloop.core.settings import SettingsStore
from tests.monitor.test_queue import farm


def client(tmp_path, *names):
    app = farm(tmp_path, *names)
    return app, TestClient(create_app(app))


def test_the_page_has_its_section_and_script(tmp_path):
    app, c = client(tmp_path)
    page = c.get("/").text
    assert 'data-panel="maintenance"' in page and 'data-tab="maintenance"' in page
    assert '<script src="/js/maintenance.js"></script>' in page
    assert c.get("/js/maintenance.js").status_code == 200
    app.shutdown()


def test_every_printer_lists_its_tasks(tmp_path):
    app, c = client(tmp_path, "a", "b")
    m = c.get("/api/maintenance").json()
    assert [p["printer"] for p in m["printers"]] == ["a", "b"] and m["due"] == 0 and m["remind"] is True
    tasks = {t["task"]: t for t in m["printers"][0]["tasks"]}
    assert len(tasks) == 9 and tasks["plate"]["every"] == 25 and tasks["plate"]["unit"] == "prints"
    assert tasks["plate"]["state"] == "ok" and tasks["plate"]["last_done"] is None and tasks["plate"]["used"] == 0
    assert tasks["screws"]["source"] == "BW" and tasks["plate"]["source"] is None
    assert [t["id"] for t in m["tasks"]][:2] == ["plate", "purge"]
    app.shutdown()


def test_done_records_it_with_a_note_and_the_log_shows_it(tmp_path):
    app, c = client(tmp_path, "a")
    c.get("/api/maintenance")  # starts counting
    for _ in range(3):
        app.store.add_outcome({"printer": "a", "outcome": "finished", "started": time.time() - 3600,
                               "ended": time.time()})
    r = c.post("/api/maintenance/done", json={"printer": "a", "task": "plate", "note": "washed"})
    assert r.status_code == 200
    item = r.json()
    assert item["task"] == "plate" and item["used"] == 0 and item["note"] == "washed" and item["last_done"]
    (row,) = c.get("/api/maintenance/log?printer=a").json()
    assert row["name"] == "Clean the build plate" and row["note"] == "washed" and row["used"]["prints"] == 3

    today = time.strftime("%Y-%m-%d")
    assert c.post("/api/maintenance/done", json={"printer": "a", "task": "belts", "date": today}).status_code == 200
    assert c.post("/api/maintenance/done", json={"printer": "a", "task": "belts", "date": "2999-01-01"}).status_code == 422
    assert c.post("/api/maintenance/done", json={"printer": "a", "task": "belts", "date": "soon"}).status_code == 422
    assert c.post("/api/maintenance/done", json={"printer": "zz", "task": "belts"}).status_code == 404
    assert c.post("/api/maintenance/done", json={"printer": "a", "task": "zz"}).status_code == 404
    app.shutdown()


def test_change_a_built_in_task_and_put_it_back(tmp_path):
    app, c = client(tmp_path, "a")
    r = c.patch("/api/maintenance/tasks/plate", json={"every": 10})
    assert r.status_code == 200 and r.json()["every"] == 10 and r.json()["unit"] == "prints"
    r = c.patch("/api/maintenance/tasks/plate", json={"unit": "days", "enabled": False})
    assert r.json() == {**r.json(), "every": 10, "unit": "days", "enabled": False}
    assert "plate" not in {i["task"] for i in c.get("/api/maintenance").json()["printers"][0]["tasks"]}
    assert SettingsStore(tmp_path).load().maintenance.edits["plate"].every == 10  # saved
    assert c.patch("/api/maintenance/tasks/plate", json={"every": 0}).status_code == 422
    assert c.patch("/api/maintenance/tasks/plate", json={"unit": "years"}).status_code == 422
    assert c.patch("/api/maintenance/tasks/nope", json={"every": 3}).status_code == 404

    assert c.delete("/api/maintenance/tasks/plate").json() == {"reset": "plate"}
    back = next(t for t in c.get("/api/maintenance").json()["tasks"] if t["id"] == "plate")
    assert (back["every"], back["unit"], back["enabled"]) == (25, "prints", True)
    app.shutdown()


def test_your_own_tasks(tmp_path):
    app, c = client(tmp_path, "a")
    r = c.post("/api/maintenance/tasks", json={"name": " Wipe the window ", "every": 48, "unit": "hours"})
    assert r.status_code == 200
    mine = r.json()
    assert mine["name"] == "Wipe the window" and mine["builtin"] is False and mine["enabled"] is True
    items = {i["task"]: i for i in c.get("/api/maintenance").json()["printers"][0]["tasks"]}
    assert items[mine["id"]]["every"] == 48 and items[mine["id"]]["unit"] == "hours"

    renamed = c.patch(f"/api/maintenance/tasks/{mine['id']}", json={"name": "Wipe the camera", "every": 3, "unit": "days"})
    assert renamed.json()["name"] == "Wipe the camera" and renamed.json()["unit"] == "days"
    assert c.post("/api/maintenance/done", json={"printer": "a", "task": mine["id"]}).status_code == 200

    assert c.post("/api/maintenance/tasks", json={"name": "", "every": 5, "unit": "days"}).status_code == 422
    assert c.post("/api/maintenance/tasks", json={"name": "x", "every": -1, "unit": "days"}).status_code == 422
    assert c.post("/api/maintenance/tasks", json={"name": "x", "every": 5, "unit": "weeks"}).status_code == 422
    assert c.delete(f"/api/maintenance/tasks/{mine['id']}").json() == {"removed": mine["id"]}
    assert c.delete(f"/api/maintenance/tasks/{mine['id']}").status_code == 404
    assert mine["id"] not in {t["id"] for t in c.get("/api/maintenance").json()["tasks"]}
    app.shutdown()


def test_reminders_can_be_turned_off(tmp_path):
    app, c = client(tmp_path, "a")
    assert c.patch("/api/maintenance", json={"remind": False}).json() == {"remind": False}
    assert SettingsStore(tmp_path).load().maintenance.remind is False
    assert c.get("/api/maintenance").json()["remind"] is False
    app.shutdown()


def test_the_summary_route_lists_what_is_due(tmp_path):
    app, c = client(tmp_path, "a")
    c.get("/api/maintenance")  # starts counting
    c.patch("/api/maintenance/tasks/plate", json={"every": 1})
    app.store.add_outcome({"printer": "a", "outcome": "finished", "started": time.time() - 5, "ended": time.time()})
    s = c.get("/api/summary").json()
    assert [(m["printer"], m["task"], m["state"]) for m in s["maintenance"]] == [("a", "plate", "due")]
    assert "Maintenance due: a: clean the build plate." in s["text"]
    assert c.get("/api/maintenance").json()["due"] == 1
    app.shutdown()
