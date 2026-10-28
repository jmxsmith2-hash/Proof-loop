"""CAD checks against example models with planted flaws (examples/cad/planted_flaws.yaml).

Every planted flaw must be found; the fixed version and the control must come back clean.
"""

from pathlib import Path

import pytest
import yaml

from synthux.core.config import load_settings
from synthux.core.db import Database
from synthux.personas.models import Persona, Traits
from synthux.physical.cad.iterate import read_measurements
from synthux.physical.cad.pipeline import check_design, design_spec, record_print

REPO = Path(__file__).resolve().parents[2]
CAD = REPO / "examples" / "cad"
TRUTH = yaml.safe_load((CAD / "planted_flaws.yaml").read_text())
SPECS = {"phone_stand_v1.stl": "phone_stand.yaml", "phone_stand_v2.stl": "phone_stand.yaml",
         "tool_handle.stl": "tool_handle.yaml", "block_control.stl": None}
PEOPLE = [Persona(id="p_small", label="Small hands", narrative="", traits=Traits(hand_size="small")),
          Persona(id="p_weak", label="Weak grip", narrative="", traits=Traits(grip_strength="low", age_range="65+")),
          Persona(id="p_large", label="Large hands", narrative="", traits=Traits(hand_size="large"))]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cad")
    settings = load_settings(REPO / "config")
    settings.data_dir = tmp
    return settings, Database(tmp / "cad.sqlite"), tmp


@pytest.mark.parametrize("name", list(TRUTH))
def test_planted_flaws_found_and_nothing_else(env, name):
    settings, db, tmp = env
    spec_file = SPECS[name]
    spec = design_spec(str(CAD / spec_file) if spec_file else f"control {name}", CAD / name)
    spec.name = f"{spec.name} ({name})"   # keep each file its own design history
    result = check_design(settings, db, CAD / name, spec, [p.model_copy(deep=True) for p in PEOPLE], tmp / name)
    found = {f["check"] for f in result["findings"] if f["severity"] >= 2}
    expected = set(TRUTH[name]["must_find"])
    assert expected <= found, f"missed {expected - found}"
    if not expected:
        assert not found, f"reported problems that are not there: {found}"
    for persona, pattern in (TRUTH[name].get("personas") or {}).items():
        pid = {"small_hands": "p_small", "low_grip": "p_weak"}[persona]
        assert pattern in [r["pattern"] for r in result["reactions"][pid]]
    assert Path(result["report"]).is_file()
    html = Path(result["report"]).read_text()
    assert "Prediction, not a test" in html or "prediction, not a test" in html


def test_iteration_loop_v1_print_v2(env):
    settings, db, tmp = env
    spec = design_spec(str(CAD / "phone_stand.yaml"), CAD / "phone_stand_v1.stl")
    spec.name = "loop test"
    v1 = check_design(settings, db, CAD / "phone_stand_v1.stl", spec, PEOPLE, tmp / "loop1")
    analysis = record_print(db, "loop test", read_measurements(CAD / "v1_print_measurements.csv"), ["poor_overhang"])
    assert any("Holes printed 0.25 mm small" in a for a in analysis["advice"])
    v2 = check_design(settings, db, CAD / "phone_stand_v2.stl", spec, PEOPLE, tmp / "loop2")
    assert v2["version"] == 2 and v2["material"]["learned_from_prints"] == 1
    assert v2["material"]["max_overhang_deg"] == 40                       # learned from the drooping print
    assert len(v2["comparison"]["fixed"]) == len(v1["findings"]) and not v2["comparison"]["new"]
    assert "What earlier prints taught us" in Path(v2["report"]).read_text()
