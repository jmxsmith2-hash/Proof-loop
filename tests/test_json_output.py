"""``--json`` on every command: one JSON document on stdout, and exit codes that mean the same everywhere."""

import json
from pathlib import Path

from proofloop.cli import main

REPO = Path(__file__).resolve().parents[1]
CAD, GCODE = REPO / "examples" / "cad", REPO / "examples" / "gcode"


def run(capsys, *args) -> tuple[int, object]:
    code = main(list(args))
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_design_check_as_json_names_every_finding_and_its_fix(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, d = run(capsys, "check", str(CAD / "phone_stand_v1.stl"), "--design", str(CAD / "phone_stand.yaml"),
                  "--json")
    assert code == 1 and d["kind"] == "design" and d["ready"] is False and d["blocking"] >= 1
    worst = max(d["findings"], key=lambda f: f["severity"])
    assert worst["id"] and worst["fix"] and worst["severity"] >= 3
    assert all(Path(p).is_file() for p in d["renders"].values()) and Path(d["report"]).is_file()
    code, d = run(capsys, "check", str(CAD / "block_control.stl"), "--json")
    assert code == 0 and d["ready"] and d["findings"] == []


def test_gcode_and_loop_files_as_json(capsys, tmp_path):
    code, d = run(capsys, "check", str(GCODE / "bracket_good.gcode.3mf"), "--json")
    assert code == 0 and d["kind"] == "gcode" and d["ready"] and d["layers"] > 0
    out = tmp_path / "made.gcode.3mf"
    code, d = run(capsys, "loop", str(GCODE / "bracket_plate.gcode.3mf"), "--loops", "3", "-o", str(out), "--json")
    assert code == 0 and d["ready"] and d["written"] == str(out.resolve()) and d["loops"] == 3 and out.is_file()
    code, d = run(capsys, "loop", "check", str(out), "--json")
    assert code == 0 and d["kind"] == "loop_check" and d["ready"]
    code, d = run(capsys, "loop", "cost", str(out), "--json")
    assert code == 0 and d["kind"] == "cost" and d["parts_per_plate"] >= 1
    code, d = run(capsys, "loop", "profiles", "--json")
    assert code == 0 and any(p["builtin"] for p in d)


def test_printers_status_and_setup_check_as_json(capsys):
    assert run(capsys, "printers", "history", "--json") == (0, [])
    code, d = run(capsys, "printers", "spools", "--json")
    assert code == 0 and d["spools"] == []
    code, d = run(capsys, "status", "--json", "--port", "8697")
    assert code == 1 and d["running"] is False
    code, d = run(capsys, "doctor", "--json")
    assert code in (0, 1) and d["checks"] and {"level", "title"} <= set(d["checks"][0])


def test_a_missing_file_exits_2(capsys):
    assert main(["check", "nothing-here.stl", "--json"]) == 2
