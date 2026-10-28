import re

import pytest
from fixtures import plain

from clearloop import threemf
from clearloop.calibration import DEFAULT_HEIGHTS, build_test
from clearloop.gcode.processor import ProcessError
from clearloop.monitor import is_loop_file
from clearloop.profiles import ProfileStore

S2 = ProfileStore().get("p2s-stage2-farmboard")
S1 = ProfileStore().get("p2s-sweep-only")


def gcode(data):
    return threemf.loads(data).gcode


def test_sweep_floor_test_pauses_after_every_height():
    data, name = build_test(threemf.loads(plain(), name="tiny.gcode.3mf"), S1, "sweep-floor")
    g = gcode(data)
    assert len(re.findall(r"^M400 U1$", g, re.M)) == len(DEFAULT_HEIGHTS) + 1
    zs = [float(z) for z in re.findall(r"^G1 Z([\d.]+) F", g[g.index("; >>> CLEARLOOP TEST") :], re.M)]
    pushes = [z for z in zs if z < max(DEFAULT_HEIGHTS) + 10]
    assert pushes == sorted(DEFAULT_HEIGHTS, reverse=True)
    assert name == "FL_S1_CALIBRATION-sweep-floor_tiny.gcode.3mf"
    assert not is_loop_file(name)  # the monitor never resumes calibration prints


def test_door_and_bend_tests_need_stage2():
    with pytest.raises(ProcessError):
        build_test(threemf.loads(plain()), S1, "door")
    g = gcode(build_test(threemf.loads(plain()), S2, "door")[0])
    assert re.findall(r"^M104 S([1-5])\b", g, re.M) == ["1", "2", "5"]


def test_cooldown_test_uses_m190_s_only():
    g = gcode(build_test(threemf.loads(plain()), S2, "cooldown", release_temp=28)[0])
    assert "M190 R" not in g and g.count("M190 S28") == S2.cooldown_repeat
