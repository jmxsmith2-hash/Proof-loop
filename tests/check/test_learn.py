"""Learning from the printer monitor's print journal (a journal is built here the way the monitor writes it)."""

import json
import sqlite3
from pathlib import Path

import pytest

from proofloop.check.gcode.pipeline import check_gcode
from proofloop.check.learn import history_for, import_prints
from proofloop.core.db import Database
from proofloop.core.paths import PRINTERS_DB

REPO = Path(__file__).resolve().parents[2]
CFG = REPO / "src" / "proofloop" / "data"
EX = REPO / "examples" / "gcode"
T0 = 1_790_000_000.0


def journal(folder: Path, records: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / PRINTERS_DB)
    conn.execute("CREATE TABLE outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, printer TEXT NOT NULL,"
                 " job TEXT, outcome TEXT NOT NULL, data TEXT NOT NULL)")
    for i, r in enumerate(records):
        rec = {"printer": "P2S-1", "model_id": "N7", "loop_no": 0, "loop_file": False, "hms": [], "print_error": None,
               "trays": [0], "filaments": [{"tray": 0, "type": "PLA", "color": "#FFFFFF"}], "eject": None,
               "file": None, "total_layers": 120, "ended": T0 + i * 3600, **r}
        conn.execute("INSERT INTO outcomes (ts, printer, job, outcome, data) VALUES (?,?,?,?,?)",
                     (rec["ended"], rec["printer"], rec["job"], rec["outcome"], json.dumps(rec)))
    conn.commit()
    conn.close()
    return folder


@pytest.fixture()
def learned(tmp_path):
    folder = journal(tmp_path / "home", [
        *({"job": "bracket_plate", "outcome": "finished", "layer": 120, "file": str(EX / "bracket_plate.gcode.3mf")}
          for _ in range(3)),
        {"job": "vase", "outcome": "failed", "layer": 2},                       # file not kept: still counted
        {"job": "hook", "outcome": "failed", "layer": 1, "hms": [{"code": "0C00-0300-0003-0008", "level": "error"}]},
        {"job": "bracket_flawed", "outcome": "failed", "layer": 50, "total_layers": 56,
         "file": str(EX / "bracket_flawed.gcode")},
        {"job": "spool_holder", "outcome": "failed", "layer": 40, "hms": [{"code": "0700-2000-0002-0001",
                                                                           "level": "error"}]},
        {"job": "FL_S2_bracket_plate", "outcome": "finished", "layer": 120, "loop_no": 1, "loop_file": True,
         "eject": "parts_left", "file": str(EX / "FL_S2_bracket_plate.gcode.3mf")},
        {"job": "bracket_plate", "outcome": "cancelled", "layer": 30},
    ])
    db = Database(tmp_path / "s.sqlite")
    counts = import_prints(db, CFG, folder)
    return db, folder, counts


def test_import_gives_each_failure_its_likely_cause(learned):
    db, folder, counts = learned
    assert counts == {"new": 9, "with_gcode": 5, "skipped": 0}
    causes = {r["job"]: r["cause"] for r in db.all("SELECT job, cause FROM print_outcomes WHERE outcome = 'failed'")}
    assert causes == {"vase": "first_layer", "hook": "first_layer", "bracket_flawed": "flow",
                      "spool_holder": "filament_feed"}
    flawed = db.one("SELECT * FROM print_outcomes WHERE job = 'bracket_flawed'")
    assert flawed["printer"] == "bambu_p2s" and flawed["material"] == "PLA"
    assert flawed["evidence_json"]["at_stop"]["peak_flow"] > 28                 # over the P2S hot end
    assert import_prints(db, CFG, folder)["new"] == 0                         # imported once


def test_history_sums_up_per_printer_and_material(learned):
    db, _, _ = learned
    h = history_for(db, "bambu_p2s", "PLA")
    assert h["prints"] == 8 and h["failed"] == 4 and h["by_cause"]["first_layer"] == 2
    assert h["proven_flow"] == pytest.approx(20.7, abs=0.2) and h["failed_flow"] > 28
    assert h["eject_checked"] == 1 and h["eject_left"] == 1


def test_the_next_check_uses_what_was_learned(learned, tmp_path):
    db, _, _ = learned
    look = lambda p, m, job: history_for(db, p, m, job)                          # noqa: E731
    flawed = check_gcode(CFG, EX / "bracket_flawed.gcode", out=tmp_path / "f", history=look)
    titles = [f["title"] for f in flawed["findings"]]
    assert "This file failed before, at layer 50" in titles
    assert "Prints have failed on your Bambu Lab P2S at this flow" in titles
    assert "2 of your last 8 PLA prints failed in the first layers" in titles
    loop = check_gcode(CFG, EX / "FL_S2_bracket_plate.gcode.3mf", out=tmp_path / "l", history=look)
    assert any(f["title"].startswith("Parts were left on the plate") for f in loop["findings"])
    assert loop["metrics"]["history"]["prints"] == 8


def test_flow_the_printer_has_finished_is_not_called_a_problem(tmp_path):
    proven = {"prints": 4, "finished": 4, "failed": 0, "by_cause": {}, "proven_flow": 70.0, "proven_prints": 3,
              "failed_flow": None, "flow_failures": 0, "eject_checked": 0, "eject_left": 0}
    r = check_gcode(CFG, EX / "bracket_flawed.gcode", out=tmp_path / "p", history=proven)
    flow = next(f for f in r["findings"] if f["check"] == "flow")
    assert flow["severity"] == 1 and "prints it fine" in flow["title"]


def test_no_journal_means_no_history(tmp_path):
    db = Database(tmp_path / "s.sqlite")
    assert import_prints(db, CFG, tmp_path / "nothing")["new"] == 0
    assert history_for(db, "bambu_p2s", "PLA")["prints"] == 0
