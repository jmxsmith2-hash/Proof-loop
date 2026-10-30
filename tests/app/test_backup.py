"""Backup and restore: everything you'd miss on a new computer, in one file."""

import io
import json
import sqlite3
import zipfile

from fastapi.testclient import TestClient
from PIL import Image

from proofloop.app.loop_server import create_app
from proofloop.core.db import Database
from proofloop.core.settings import PrinterConfig
from proofloop.monitor.service import App
from tests.app.test_loop_server import FakeLink


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "gray").save(buf, "JPEG")
    return buf.getvalue()


def setup(folder):
    app = App(folder, connect=False, link_factory=FakeLink)
    app.add_printer(PrinterConfig(name="p2s", host="1.2.3.4", serial="S", access_code="x", model_id="N7"))
    app.verifier(app.settings.printers[0]).add_reference(jpeg())
    (folder / "references" / "p2s" / "last-diff.png").write_bytes(b"not kept")
    app.queue.add("plate.gcode.3mf", b"PLATE", {"loops": 3}, {"parts": 6, "left_out": [],
                  "rows": [{"printer": "p2s", "loops": 3}]})
    (folder / "config").mkdir()
    (folder / "config" / "settings.yaml").write_text("log_level: DEBUG\n", encoding="utf-8")
    (folder / "feedback.json").write_text('[{"check": "overhang"}]', encoding="utf-8")
    db = Database(folder / "results.db")
    db.execute("CREATE TABLE IF NOT EXISTS kept (v TEXT)")
    db.execute("INSERT INTO kept VALUES ('a design check')")
    return app, db


def test_backup_holds_everything_and_restores_it_onto_a_new_computer(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(), new.mkdir()
    app, _ = setup(old)
    r = TestClient(create_app(app)).get("/api/backup")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert {"backup.json", "results.db", "feedback.json", "config/settings.yaml",
            "references/p2s/ref-00.jpg", "queue/queue.json"} <= names
    assert "references/p2s/last-diff.png" not in names and "phone.json" not in names

    fresh = App(new, connect=False, link_factory=FakeLink)
    live = Database(new / "results.db")  # the window has it open while you restore
    c = TestClient(create_app(fresh))
    out = c.post("/api/restore", files={"file": ("b.zip", r.content)}).json()
    assert out == {"restored": True, "spools": 0, "printers": 1, "queued": 1, "plate_pictures": 1}
    assert "p2s" in fresh.watches and fresh.queue.plate(fresh.queue.runs()[0]["id"])[0] == b"PLATE"
    assert (new / "config" / "settings.yaml").read_text(encoding="utf-8") == "log_level: DEBUG\n"
    assert json.loads((new / "feedback.json").read_text(encoding="utf-8")) == [{"check": "overhang"}]
    assert [tuple(r) for r in live.execute("SELECT v FROM kept")] == [("a design check",)]


def test_an_older_json_backup_still_restores(tmp_path):
    app, _ = setup(tmp_path)
    zipped = zipfile.ZipFile(io.BytesIO(TestClient(create_app(app)).get("/api/backup").content))
    old_style = zipped.read("backup.json")
    app.remove_printer("p2s")
    r = TestClient(create_app(app)).post("/api/restore", files={"file": ("b.json", old_style)})
    assert r.status_code == 200 and r.json()["printers"] == 1 and "p2s" in app.watches


def zip_of(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_a_backup_that_could_write_anywhere_else_is_refused(tmp_path):
    app = App(tmp_path, connect=False, link_factory=FakeLink)
    c = TestClient(create_app(app))
    good = json.dumps({"proofloop_backup": 1})
    for bad in ({"backup.json": good, "../outside.txt": "x"}, {"backup.json": good, "phone.json": "{}"},
                {"backup.json": good, "results.db": "not a database"}, {"other.json": good},
                {"backup.json": "{}"}):
        r = c.post("/api/restore", files={"file": ("b.zip", zip_of(bad))})
        assert r.status_code == 422, bad
    assert not (tmp_path.parent / "outside.txt").exists()


def test_a_phone_can_download_a_backup_but_not_restore_one(tmp_path):
    app, _ = setup(tmp_path)
    phone = TestClient(create_app(app, allow_send=False))
    backup = phone.get("/api/backup")
    assert backup.status_code == 200
    assert phone.post("/api/restore", files={"file": ("b.zip", backup.content)}).status_code == 403
    assert not phone.get("/api/state").json()["can_send"]


def test_the_copy_of_results_is_a_working_database(tmp_path):
    app, db = setup(tmp_path)
    data = zipfile.ZipFile(io.BytesIO(TestClient(create_app(app)).get("/api/backup").content)).read("results.db")
    (tmp_path / "copy.db").write_bytes(data)
    con = sqlite3.connect(tmp_path / "copy.db")
    assert con.execute("SELECT v FROM kept").fetchall() == [("a design check",)]
    con.close()
