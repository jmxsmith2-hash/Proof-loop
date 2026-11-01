"""queue.json: an old bare list still loads, saves carry a version, a newer file is left alone."""

import json

import pytest

from proofloop.core import schema
from proofloop.monitor.queue import Queue

RUN = {"id": "r1", "plate": "p.gcode.3mf", "parts": 4, "created": 1.0, "settings": {},
       "jobs": [{"id": "j1", "printer": "a", "loops": 2, "status": "waiting", "notified": False,
                 "file": None, "at": None}]}
PLAN = {"rows": [{"printer": "a", "loops": 3}], "left_out": [], "parts": 6}


def test_an_old_bare_list_loads_as_before(tmp_path):
    (tmp_path / "queue.json").write_text(json.dumps([RUN]), encoding="utf-8")
    q = Queue(tmp_path)
    assert q.runs() == [RUN] and q.get("r1")["jobs"][0]["id"] == "j1"


def test_the_first_save_keeps_a_copy_and_writes_the_version(tmp_path):
    (tmp_path / "queue.json").write_text(json.dumps([RUN]), encoding="utf-8")
    q = Queue(tmp_path)
    q.mark("r1", "j1", status="sent")
    doc = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1 and doc["runs"][0]["jobs"][0]["status"] == "sent"
    assert json.loads((tmp_path / "queue.json.v0.bak").read_text(encoding="utf-8")) == [RUN]
    q.mark("r1", "j1", status="done")
    assert json.loads((tmp_path / "queue.json.v0.bak").read_text(encoding="utf-8")) == [RUN]
    assert q.job("r1", "j1")["status"] == "done"
    assert not list(tmp_path.glob("*.tmp"))


def test_a_new_queue_is_versioned_from_the_start(tmp_path):
    q = Queue(tmp_path / "queue")
    run = q.add("p.gcode.3mf", b"plate", {"loops": 9, "x": 1}, PLAN)
    doc = json.loads(q.path.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1 and doc["runs"] == [run]
    assert sorted(p.name for p in q.folder.iterdir()) == [f"{run['id']}.gcode.3mf", "queue.json"]


def test_a_newer_queue_can_be_read_but_nothing_is_written_over_it(tmp_path):
    newer = {"schema_version": 2, "runs": [RUN]}
    (tmp_path / "queue.json").write_text(json.dumps(newer), encoding="utf-8")
    q = Queue(tmp_path)
    assert q.runs() == [RUN]
    for change in (lambda: q.mark("r1", "j1", status="sent"), lambda: q.remove("r1"),
                   lambda: q.add("p.gcode.3mf", b"plate", {}, PLAN)):
        with pytest.raises(schema.NewerVersionError, match="queue.json was written by a newer ProofLoop"):
            change()
    assert json.loads((tmp_path / "queue.json").read_text(encoding="utf-8")) == newer
    assert [p.name for p in tmp_path.iterdir()] == ["queue.json"]  # no plate left behind by a refused add
