"""Checks against the real, public FarmLoop P2S files (not shipped with ProofLoop).

git clone https://github.com/Liaminamerica2/p2s-autofarm-playbook
PROOFLOOP_EVIDENCE=p2s-autofarm-playbook/evidence pytest tests/test_evidence.py
"""

import json
import os
import re
from dataclasses import asdict
from pathlib import Path

import pytest

from proofloop.loopfiles import threemf
from proofloop.loopfiles.gcode.processor import process
from proofloop.loopfiles.gcode.routines import LoopSettings
from proofloop.loopfiles.importer import import_profile
from proofloop.loopfiles.lint import lint
from proofloop.loopfiles.profiles import BUILTIN_DIR, ProfileStore

EV = os.environ.get("PROOFLOOP_EVIDENCE")
pytestmark = pytest.mark.skipif(not EV, reason="set PROOFLOOP_EVIDENCE to the playbook's evidence folder")
PLAIN = "Poop-Bucket-Logo-V2(5)_Poop Bucket.gcode.3mf"
FL = "FL_S2_Poop-Bucket-Logo-V2(5)_Poop Bucket.gcode.3mf"


def test_builtin_profile_matches_the_vendor_file():
    r = import_profile(threemf.load(Path(EV) / PLAIN), threemf.load(Path(EV) / FL))
    shipped = json.loads((BUILTIN_DIR / "p2s-stage2-farmboard.json").read_text())
    got = asdict(r.profile)
    for k in (
        "start_block",
        "door_open",
        "bend",
        "safety",
        "door_close",
        "cooldown_fan",
        "cooldown_repeat",
        "fans_off",
        "sweep",
        "start_anchor",
    ):
        assert got[k] == shipped[k], k


def test_real_slice_end_to_end():
    sf = threemf.load(Path(EV) / PLAIN)
    r = process(sf, ProfileStore().get("p2s-stage2-farmboard"), LoopSettings(loops=2, wait_for_monitor=True))
    parts = {p.name: p.height for p in r.analysis.parts}
    assert parts == {"Bucket Bambu Logo": 170.0, "Clip - V2": 5.0}
    assert all(r.analysis.plan.part_push[p.label] < p.height for p in r.analysis.parts)
    out = threemf.loads(r.data, name=r.filename)
    assert out.md5_ok and not any(f.level == "danger" for f in lint(out))
    assert re.findall(r"^M104 S([1-5])\b", out.gcode, re.M) == ["1", "2", "3", "4", "5"] * 2


def test_vendor_file_problems_are_found():
    titles = {f.title for f in lint(threemf.load(Path(EV) / FL))}
    assert "Loop 1: 120 min wait before printing, heaters on" in titles
    assert "Parts the sweep passes over" in titles


def test_gcode_extrusion_matches_the_slicers_grams():
    from proofloop.loopfiles.gcode.usage import layer_usage

    sf = threemf.load(Path(EV) / PLAIN)
    p = sf.info.presets["1"]
    assert p["preset"] == "Bambu PLA Basic @BBL P2S" and p["density"] == 1.26 and p["filament_id"] == "GFA00"
    u = layer_usage(sf.gcode, ["1"], {"1": p["diameter"]}, {"1": p["density"]})
    assert u["layers"] == 850
    assert u["grams"]["1"][-1] == pytest.approx(sf.info.slot_grams["1"], abs=0.02)  # 53.887 vs 53.89
