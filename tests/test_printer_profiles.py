"""The built-in printer profiles (src/proofloop/data/printers.yaml) have what the checks read, and every
Bambu model with a profile is found from its slicer name by exactly one profile."""

from collections import Counter

import pytest

from proofloop.check.cad.load import load_profiles
from proofloop.check.gcode.pipeline import printer_from_header
from proofloop.check.my_printers import profile_for_model
from proofloop.core.config import load_yaml_file
from proofloop.core.paths import DEFAULTS
from proofloop.core.printers import MODEL_IDS

DATA = load_yaml_file(DEFAULTS / "printers.yaml")
PRINTERS = DATA["printers"]
FDM = {k: v for k, v in PRINTERS.items() if v.get("type") == "fdm"}

# Bambu models that have a design-check profile; the rest of MODEL_IDS is monitor-only for now.
PROFILED = ["X1 Carbon", "X1", "X1E", "P1P", "P1S", "A1 mini", "A1", "H2D", "H2D Pro", "H2S", "P2S", "H2C", "X2D", "A2L"]


def test_default_printer_exists():
    assert DATA["default_printer"] in PRINTERS


@pytest.mark.parametrize("key", sorted(PRINTERS))
def test_every_profile_has_what_the_code_reads(key):
    p = PRINTERS[key]
    assert p["type"] in ("fdm", "resin")
    assert len(p["build_volume"]) == 3 and all(float(v) > 0 for v in p["build_volume"])
    assert float(p["nozzle"]) > 0 and float(p["layer_height"]) > 0 and float(p["volumetric_speed_mm3_s"]) > 0
    if p["type"] == "fdm":
        assert isinstance(p["names"], list) and all(isinstance(n, str) and n for n in p["names"])
        assert p["extruder"] in ("direct", "bowden")
        for field in ("accel_mm_s2", "max_flow_mm3_s", "max_nozzle_temp", "max_bed_temp"):
            assert float(p[field]) > 0, field
        assert float(p["max_flow_mm3_s"]) > float(p["volumetric_speed_mm3_s"]), "the average can't beat the hot end"
        assert float(p["max_nozzle_temp"]) > float(p["max_bed_temp"])
        assert isinstance(p.get("enclosed", False), bool)
    # the loader the design and G-code checks use accepts it
    pr, _mat, _slicer = load_profiles(DEFAULTS, key, "PLA")
    assert pr["name"] == key


def test_no_two_profiles_claim_the_same_name():
    claims = Counter(n.lower() for p in FDM.values() for n in p["names"])
    assert [n for n, c in claims.items() if c > 1] == []


def test_bambu_profiles_use_the_slicer_name_prefix():
    for key, p in FDM.items():
        if key.startswith("bambu_"):
            assert all(n.startswith("Bambu Lab ") for n in p["names"]), key


@pytest.mark.parametrize("model", PROFILED)
def test_each_bambu_model_resolves_to_exactly_one_profile(model):
    assert model in MODEL_IDS.values(), f"{model} isn't a model proofloop.core.printers knows"
    name = f"Bambu Lab {model}"
    owners = [k for k, p in FDM.items() if name.lower() in (n.lower() for n in p["names"])]
    assert len(owners) == 1, owners
    # from a G-code header (printer_model) ...
    assert printer_from_header(DEFAULTS, name) == owners[0]
    assert printer_from_header(DEFAULTS, name.upper()) == owners[0]
    # ... and from a saved printer's model id
    model_id = next(i for i, m in MODEL_IDS.items() if m == model)
    assert profile_for_model(DATA, model_id) == owners[0]


def test_x1_key_still_covers_the_x1_family():
    assert set(PRINTERS["bambu_x1"]["names"]) == {"Bambu Lab X1 Carbon", "Bambu Lab X1", "Bambu Lab X1E"}
    assert PRINTERS["bambu_p1s"]["names"] == ["Bambu Lab P1S"] and PRINTERS["bambu_p1p"]["names"] == ["Bambu Lab P1P"]


def test_open_printers_are_not_marked_enclosed():
    for key in ("bambu_p1p", "bambu_a1", "bambu_a1_mini", "bambu_a2l"):
        assert not PRINTERS[key].get("enclosed"), key
    for key in ("bambu_x1", "bambu_p1s", "bambu_p2s", "bambu_h2d", "bambu_h2s", "bambu_h2c", "bambu_x2d"):
        assert PRINTERS[key]["enclosed"] is True, key
