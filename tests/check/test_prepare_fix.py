"""What `prepare(fix=True)` reports: each setting's old and new value, the presets to import, the report."""

import json
from pathlib import Path

from proofloop.check.prepare import prepare
from proofloop.core.config import load_settings
from proofloop.core.db import Database
from tests.check.test_prepare import CFG, REPO, profiles  # noqa: F401 - the fake slicer and its preset tree


def test_fix_reports_old_and_new_values_and_writes_presets_to_import(profiles, tmp_path):  # noqa: F811
    settings = load_settings(CFG).model_copy(update={"data_dir": tmp_path / "data"})
    r = prepare(settings, Database(tmp_path / "data" / "s.sqlite"), REPO / "examples" / "cad" / "phone_stand_v1.stl",
                fix=True, out=tmp_path / "fix", personas=2)
    support = next(s for s in r["rounds"][0]["applied"] if s["key"] == "enable_support")
    assert (support["old"], support["value"], support["preset"]) == ("0", "1", "process")
    assert support["why"] and support["label"] == "Process › Support › Enable support"
    assert r["fix_presets"]["process"] == "slicer-fixes/process.json" and r["links"]["fix_presets"] == r["fix_presets"]
    assert set(r["fix_presets"]) <= {"process", "filament"}
    preset = json.loads((tmp_path / "fix" / r["fix_presets"]["process"]).read_text(encoding="utf-8"))
    assert preset["enable_support"] == "1" and preset["type"] == "process"
    assert preset["inherits"] == "0.20mm Standard @BBL P2S" and preset["from"] == "User"
    html = Path(r["report"]).read_text(encoding="utf-8")
    assert "Fixed and sliced again" in html and "Enable support" in html and 'href="slicer-fixes/process.json"' in html
    assert "(support on)" in html
    assert all(f["severity_name"] for f in r["slicer_findings"] + r["gcode"]["findings"])
