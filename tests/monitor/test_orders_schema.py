"""orders.json: an old bare list still loads, saves carry a version, a newer file is left alone."""

import json

import pytest

from proofloop.core import schema
from proofloop.monitor.orders import Orders

ORDER = {"id": "o1", "customer": "Sam", "plate": "p", "quantity": 10, "due": None, "price_each": None,
         "cost_each": None, "notes": "", "created": 1.0, "status": "open", "run": "r1", "parts_per_loop": 2,
         "loop_s": {"a": 60.0}, "told": False}


def test_an_old_bare_list_loads_as_before(tmp_path):
    (tmp_path / "orders.json").write_text(json.dumps([ORDER]), encoding="utf-8")
    o = Orders(tmp_path / "orders.json")
    assert o.all() == [ORDER] and o.get("o1")["customer"] == "Sam"


def test_the_first_save_keeps_a_copy_and_writes_the_version(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text(json.dumps([ORDER]), encoding="utf-8")
    o = Orders(path)
    o.update("o1", status="done")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc == {"schema_version": 1, "orders": [{**ORDER, "status": "done"}]}
    bak = tmp_path / "orders.json.v0.bak"
    assert json.loads(bak.read_text(encoding="utf-8")) == [ORDER]
    o.update("o1", told=True)
    assert json.loads(bak.read_text(encoding="utf-8")) == [ORDER]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["orders.json", "orders.json.v0.bak"]


def test_a_newer_file_can_be_read_but_nothing_is_written_over_it(tmp_path):
    path = tmp_path / "orders.json"
    newer = {"schema_version": 3, "orders": [ORDER]}
    path.write_text(json.dumps(newer), encoding="utf-8")
    o = Orders(path)
    assert o.all() == [ORDER]
    with pytest.raises(schema.NewerVersionError, match="update ProofLoop"):
        o.update("o1", status="done")
    with pytest.raises(schema.NewerVersionError):
        o.remove("o1")
    assert json.loads(path.read_text(encoding="utf-8")) == newer
