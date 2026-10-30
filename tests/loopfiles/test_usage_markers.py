"""Filament per layer when the slicer writes no layer numbers (M73 L): the "; CHANGE_LAYER" comments count."""

from pathlib import Path

from proofloop.loopfiles import threemf
from proofloop.loopfiles.gcode.usage import layer_usage, used_by_layer

PLATE = Path(__file__).resolve().parents[2] / "examples" / "gcode" / "bracket_plate.gcode.3mf"


def test_layers_are_counted_from_the_change_layer_comments():
    sf = threemf.load(PLATE)
    assert "\nM73 L" not in sf.gcode
    u = layer_usage(sf.gcode, ["1"], {"1": 1.75}, {"1": 1.24})
    assert u["layers"] == sf.gcode.count("; CHANGE_LAYER") == 120
    assert abs(u["grams"]["1"][-1] - sf.info.slot_grams["1"]) < 0.5  # the whole plate: what the slicer says
    assert 0 < used_by_layer(u, 60)["1"] < u["grams"]["1"][-1]


def test_m73_layer_numbers_win_when_there_are_some():
    g = "; HEADER\nM83\n; EXECUTABLE_BLOCK_START\n; CHANGE_LAYER\nM73 L1\nG1 X1 E10\n; CHANGE_LAYER\nM73 L2\nG1 X2 E5\n" \
        "; EXECUTABLE_BLOCK_END\n"
    u = layer_usage(g, ["1"], {"1": 1.75}, {"1": 1.24})
    assert u["layers"] == 2
