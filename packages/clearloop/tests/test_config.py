from datetime import datetime
from pathlib import Path

import pytest

from clearloop.config import QuietHours, load_config
from clearloop.farm import process_options
from clearloop.schedule import in_quiet_hours

EXAMPLE = Path(__file__).parent.parent / "examples" / "config.example.yaml"


def test_example_config_loads_and_merges_overrides():
    cfg = load_config(EXAMPLE)
    p1s = cfg.printer("p1s-01")
    opts = process_options(cfg, p1s)
    assert opts.loop.eject_mode == "board" and opts.loop.door_opener is True
    assert opts.loop.sweep.lane_spacing == 25
    assert process_options(cfg, cfg.printer("a1-01")).loop.eject_mode == "gcode"
    swap = cfg.printer("a1mini-swap")
    assert process_options(cfg, swap).loop.eject_mode == "custom"
    assert cfg.verify_for(swap).roi == (0.1, 0.2, 0.9, 0.95)


def test_unknown_model_fails_early(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("printers: [{name: x, model: ender3, host: h, serial: s, access_code: a}]")
    with pytest.raises(KeyError):
        load_config(bad)


def test_quiet_hours_across_midnight():
    q = QuietHours(start="22:00", end="07:00")
    assert in_quiet_hours(q, datetime(2026, 1, 1, 23, 30))
    assert in_quiet_hours(q, datetime(2026, 1, 1, 6, 59))
    assert not in_quiet_hours(q, datetime(2026, 1, 1, 12, 0))
    assert not in_quiet_hours(None)
