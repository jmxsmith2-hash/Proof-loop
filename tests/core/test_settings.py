"""settings.json: versioned, atomic, and never written over when a newer ProofLoop made it."""

import json

import pytest

from proofloop.core import schema
from proofloop.core.settings import PrinterConfig, Settings, SettingsStore

OLD = {
    "defaults": {"loops": 3, "floor_z": 2.5},
    "printers": [{"name": "a", "host": "10.0.0.2", "serial": "01", "access_code": "12345678", "model_id": "N7"}],
    "send_to_printer": True,
}


def test_an_old_file_without_a_version_loads_as_before(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps(OLD), encoding="utf-8")
    s = SettingsStore(tmp_path).load()
    assert s == Settings.model_validate(OLD)
    assert s.defaults.loops == 3 and s.printers[0].name == "a" and s.send_to_printer


def test_saving_writes_the_version_and_keeps_a_copy_of_the_old_file_once(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(OLD), encoding="utf-8")
    store = SettingsStore(tmp_path)
    s = store.load()
    store.save(s)
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1
    bak = tmp_path / "settings.json.v0.bak"
    assert json.loads(bak.read_text(encoding="utf-8")) == OLD
    s.defaults.loops = 7
    store.save(s)  # now version 1: nothing more is copied
    assert json.loads(bak.read_text(encoding="utf-8")) == OLD
    assert store.load().defaults.loops == 7
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json", "settings.json.v0.bak"]


def test_save_round_trips_and_leaves_no_temp_file(tmp_path):
    store = SettingsStore(tmp_path / "new")
    s = Settings()
    s.printers.append(PrinterConfig(name="a", host="h", serial="1", access_code="x", model_id="N7"))
    store.save(s)
    assert store.load() == s
    assert [p.name for p in store.dir.iterdir()] == ["settings.json"]
    assert next(iter(json.loads(store.path.read_text(encoding="utf-8")))) == "schema_version"


def test_a_newer_file_loads_what_it_can_but_is_not_saved_over(tmp_path):
    path = tmp_path / "settings.json"
    newer = {"schema_version": 5, "defaults": {"loops": 4}, "something_new": [1]}
    path.write_text(json.dumps(newer), encoding="utf-8")
    store = SettingsStore(tmp_path)
    s = store.load()
    assert s.defaults.loops == 4
    with pytest.raises(schema.NewerVersionError) as e:
        store.save(s)
    assert str(e.value).startswith("settings.json was written by a newer ProofLoop")
    assert "update ProofLoop (proofloop update) before changing settings" in str(e.value)
    assert json.loads(path.read_text(encoding="utf-8")) == newer
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]
