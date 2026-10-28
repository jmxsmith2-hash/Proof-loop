import sys

import pytest
from fixtures import farmloop_3mf, plain

from clearloop import threemf
from clearloop.gcode.processor import process
from clearloop.gcode.routines import LoopSettings
from clearloop.print_checks import print_checks
from clearloop.profiles import ProfileStore

ONE = [("1", 100.0, 100.0, 140.0, 140.0, 12.0)]


def _looped(loops=2, boxes=None):
    sf = threemf.loads(plain(boxes) if boxes else plain(), name="plate.gcode.3mf")
    r = process(sf, ProfileStore().get("p2s-stage2-farmboard"), LoopSettings(loops=loops))
    return threemf.loads(r.data, name=r.filename)


def _hide_synthux(monkeypatch):
    for name in ("synthux", "synthux.physical", "synthux.physical.gcode"):   # also once already imported
        monkeypatch.setitem(sys.modules, name, None)


def test_without_synthux_nothing_changes(monkeypatch):
    _hide_synthux(monkeypatch)
    assert print_checks(_looped()) is None


def test_synthux_checks_the_print_in_each_loop_not_the_sweep():
    pytest.importorskip("synthux")
    found = print_checks(_looped(3, ONE))
    assert found and all(f.title.startswith("Print") for f in found)
    titles = " ".join(f.title for f in found)
    assert "ClearLoop" not in titles                     # the ejection cycle is lint's job, not repeated
    assert "Travels over open space" not in titles       # one part: only the sweep could cross open space
    two = " ".join(f.title for f in print_checks(_looped(3)))
    assert "Travels over open space without retracting (loop 1, layer 1)" in two   # hops between two parts
    # the synthetic test plate never turns its part-cooling fan on, which a real PLA print needs
    fan = next(f for f in found if "fan" in f.title.lower())
    assert fan.level == "warning"


def test_cli_and_api_show_print_checks(tmp_path, capsys):
    pytest.importorskip("synthux")
    from clearloop.cli import main

    path = tmp_path / "FL_S2_b.gcode.3mf"
    path.write_bytes(farmloop_3mf())
    main(["check", str(path)])
    out = capsys.readouterr().out
    assert "Print checks (synthux):" in out and "natural cooldown" in out


def test_api_check_includes_print_findings(tmp_path, monkeypatch):
    from test_server import client, up

    c, _ = client(tmp_path)
    with monkeypatch.context() as m:
        _hide_synthux(m)
        r = up(c, farmloop_3mf(), "FL_S2_b.gcode.3mf", "/api/check").json()
    assert r["print_findings"] is None and r["findings"]          # ClearLoop's own checks as before
    pytest.importorskip("synthux")
    r = up(c, farmloop_3mf(), "FL_S2_b.gcode.3mf", "/api/check").json()
    assert r["print_findings"] and all(f["title"].startswith("Print") for f in r["print_findings"])
