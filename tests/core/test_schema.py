"""Versions for the JSON files: older ones upgrade in memory, newer ones are never written over."""

import json

import pytest

from proofloop.core import schema


def test_every_kind_starts_at_version_one():
    assert [schema.current(k) for k in schema.KINDS] == [1, 1, 1]


def test_no_version_means_version_zero_and_a_list_is_always_zero():
    assert schema.version_of({"printers": []}) == 0
    assert schema.version_of([{"id": "x"}]) == 0
    assert schema.version_of({"schema_version": 3}) == 3
    for bad in ("1", True, -1, 1.5):
        with pytest.raises(ValueError, match="can't read"):
            schema.version_of({"schema_version": bad}, "queue.json")


def test_upgrade_from_zero_adds_the_version_and_wraps_the_lists():
    doc, found = schema.upgrade("settings", {"loops": 2}, "settings.json")
    assert found == 0 and doc == {"loops": 2, "schema_version": 1}
    doc, found = schema.upgrade("queue", [{"id": "r1"}], "queue.json")
    assert found == 0 and doc == {"runs": [{"id": "r1"}], "schema_version": 1}
    doc, _ = schema.upgrade("orders", {"not": "a list"}, "orders.json")
    assert doc["orders"] == []
    doc, found = schema.upgrade("orders", {"schema_version": 1, "orders": [1]}, "orders.json")
    assert found == 1 and doc == {"schema_version": 1, "orders": [1]}


@pytest.fixture
def two_steps(monkeypatch):
    """A pretend future: settings at version 3, with 1->2 and 2->3 on top of the real 0->1."""
    monkeypatch.setitem(schema._STEPS, "settings", list(schema._STEPS["settings"]))
    seen = []

    @schema.step("settings", 1)
    def _rename(doc):
        seen.append(1)
        doc["farm"] = doc.pop("printers", [])
        return doc

    @schema.step("settings", 2)
    def _count(doc):
        seen.append(2)
        doc["count"] = len(doc["farm"])
        return doc

    return seen


def test_migrations_run_in_order_from_the_files_version(two_steps):
    assert schema.current("settings") == 3
    doc, found = schema.upgrade("settings", {"printers": ["a", "b"]}, "settings.json")
    assert found == 0 and two_steps == [1, 2]
    assert doc == {"farm": ["a", "b"], "count": 2, "schema_version": 3}
    two_steps.clear()
    doc, found = schema.upgrade("settings", {"schema_version": 2, "farm": ["a"]}, "settings.json")
    assert found == 2 and two_steps == [2] and doc["count"] == 1


def test_steps_must_be_added_in_order(monkeypatch):
    monkeypatch.setitem(schema._STEPS, "settings", list(schema._STEPS["settings"]))
    with pytest.raises(ValueError, match="next step"):
        schema.step("settings", 5)(lambda doc: doc)


def test_a_newer_file_is_refused_in_plain_words():
    with pytest.raises(schema.NewerVersionError) as e:
        schema.upgrade("settings", {"schema_version": 9}, "settings.json")
    assert str(e.value) == ("settings.json was written by a newer ProofLoop (its format 9, this one knows 1); "
                            "update ProofLoop (proofloop update) before changing settings")
    doc, found = schema.upgrade("settings", {"schema_version": 9, "x": 1}, "settings.json", newer_ok=True)
    assert found == 9 and doc == {"schema_version": 9, "x": 1}


def test_check_write_keeps_one_copy_of_an_older_file(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text("[1]", encoding="utf-8")
    schema.check_write("orders", path)
    bak = tmp_path / "orders.json.v0.bak"
    assert bak.read_text(encoding="utf-8") == "[1]"
    path.write_text("[2]", encoding="utf-8")
    schema.check_write("orders", path)  # already kept: the first copy stays
    assert bak.read_text(encoding="utf-8") == "[1]"


def test_check_write_leaves_current_missing_and_damaged_files_alone(tmp_path):
    path = tmp_path / "orders.json"
    schema.check_write("orders", path)
    path.write_text('{"schema_version": 1, "orders": []}', encoding="utf-8")
    schema.check_write("orders", path)
    path.write_text("{ not json", encoding="utf-8")
    schema.check_write("orders", path)
    assert list(tmp_path.iterdir()) == [path]


def test_check_write_refuses_a_newer_file(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text('{"schema_version": 2, "runs": []}', encoding="utf-8")
    with pytest.raises(schema.NewerVersionError, match="queue.json was written by a newer ProofLoop"):
        schema.check_write("queue", path)


def test_write_json_leaves_no_temp_file_even_when_it_fails(tmp_path):
    path = tmp_path / "sub" / "x.json"
    schema.write_json(path, {"a": "é"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": "é"}
    with pytest.raises(TypeError):
        schema.write_json(path, {"a": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": "é"}  # the old file is untouched
    assert [p.name for p in path.parent.iterdir()] == ["x.json"]


def test_stamp_puts_the_version_first():
    assert list(schema.stamp("queue", {"runs": [], "schema_version": 0})) == ["schema_version", "runs"]
    assert schema.stamp("queue", {"runs": []})["schema_version"] == 1


def test_restore_member_upgrades_an_older_backup_file():
    out = schema.restore_member("orders.json", b'[{"id": "o1"}]')
    assert json.loads(out) == {"schema_version": 1, "orders": [{"id": "o1"}]}
    out = schema.restore_member("queue/queue.json", b'[{"id": "r1"}]')
    assert json.loads(out)["runs"] == [{"id": "r1"}]


def test_restore_member_passes_other_files_and_current_files_through():
    assert schema.restore_member("feedback.json", b"{ anything") == b"{ anything"
    assert schema.restore_member("queue/abc.gcode.3mf", b"\x00zip") == b"\x00zip"
    current = b'{"schema_version": 1, "orders": []}'
    assert schema.restore_member("orders.json", current) == current


def test_restore_member_refuses_a_newer_backup_and_a_damaged_file():
    with pytest.raises(ValueError, match=r"The backup's orders.json was written by a newer ProofLoop.*"
                                         r"update ProofLoop \(proofloop update\) before restoring it"):
        schema.restore_member("orders.json", b'{"schema_version": 4, "orders": []}')
    with pytest.raises(ValueError, match="damaged"):
        schema.restore_member("orders.json", b"{ nope")


def test_restore_settings():
    assert schema.restore_settings({"loops": 2}) == {"loops": 2}  # an older backup, no version
    assert schema.restore_settings({"schema_version": 1, "loops": 2}) == {"loops": 2}
    assert schema.restore_settings(None) is None
    with pytest.raises(schema.NewerVersionError, match="before restoring it"):
        schema.restore_settings({"schema_version": 7})
