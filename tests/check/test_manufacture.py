"""Injection moulding and CNC checks, on parts built from boxes."""

from pathlib import Path

import pytest
import trimesh
from typer.testing import CliRunner

from proofloop.check.cad.load import Model
from proofloop.check.cad.manufacture import load_process, run_process_checks
from proofloop.check.cli import app
from proofloop.core.config import ConfigError

REPO = Path(__file__).resolve().parents[2]
CFG = REPO / "src" / "proofloop" / "data"


def box(size, centre):
    b = trimesh.creation.box(size)
    b.apply_translation(centre)
    return b


def model(parts):
    mesh = trimesh.util.concatenate(parts) if isinstance(parts, list) else parts
    return Model(path=Path("t.stl"), mesh=mesh, file_hash="t")


def run(parts, process, material=None, **kw):
    proc, mat = load_process(CFG, process, material)
    return run_process_checks(model(parts), proc, mat, {}, **kw)


def checks(ctx):
    return {f.check: f for f in ctx.findings}


def tray(window=False):
    """An open-topped box with 2 mm walls; optionally a window through one end wall."""
    p = [box((60, 40, 2), (30, 20, 1)), box((60, 2, 20), (30, 1, 12)), box((60, 2, 20), (30, 39, 12)),
         box((2, 36, 20), (59, 20, 12))]
    if window:
        p += [box((2, 36, 8), (1, 20, 6)), box((2, 36, 6), (1, 20, 19)), box((2, 13, 6), (1, 8.5, 13)),
              box((2, 13, 6), (1, 31.5, 13))]
    else:
        p.append(box((2, 36, 20), (1, 20, 12)))
    return p


def block_with_pocket(px, py, pw, pd, depth, size=(50, 40, 30)):
    W, D, H = size
    z = H - depth / 2
    return [box((W, D, H - depth), (W / 2, D / 2, (H - depth) / 2)),
            box((px - pw / 2, D, depth), ((px - pw / 2) / 2, D / 2, z)),
            box((W - px - pw / 2, D, depth), ((W + px + pw / 2) / 2, D / 2, z)),
            box((pw, py - pd / 2, depth), (px, (py - pd / 2) / 2, z)),
            box((pw, D - py - pd / 2, depth), (px, (D + py + pd / 2) / 2, z))]


def sealed():
    inner = box((20, 20, 10), (20, 15, 10))
    inner.invert()
    return [box((40, 30, 20), (20, 15, 10)), inner]


def test_moulding_tray_needs_draft_and_a_side_action_only_for_the_window():
    plain = run(tray(), "injection")
    assert plain.metrics["pull"] == "z" and "undercut" not in checks(plain)
    assert checks(plain)["draft"].severity == 3                                  # boxes have no draft at all
    assert 1.8 < plain.metrics["wall_mm"]["typical"] < 2.6 and "wall_thickness" not in checks(plain)
    window = checks(run(tray(window=True), "injection"))["undercut"]
    assert window.severity == 2 and window.data["opens"] == ["-X"]


def test_moulding_thick_sections_sealed_spaces_and_estimates():
    solid = run(box((40, 30, 20), (20, 15, 10)), "injection")
    assert checks(solid)["wall_thickness"].severity == 3                         # 20 mm solid block
    stats = dict((label.split(" (")[0], value) for value, label in solid.metrics["estimates"]["process_stats"])
    assert stats["shot weight"] == "25.0 g" and stats["clamp force"] == "5 t"
    closed = checks(run(sealed(), "injection"))["undercut"]
    assert closed.severity == 4 and "can't form" in closed.title
    with pytest.raises(ConfigError, match="unknown injection material"):
        load_process(CFG, "injection", "PLA")


def test_cnc_reach_tool_size_and_pockets():
    plain = run(box((50, 40, 30), (25, 20, 15)), "cnc")
    assert not plain.findings and plain.metrics["setups"] == []
    deep = checks(run(block_with_pocket(25, 20, 5, 20, 25), "cnc"))
    assert deep["deep_pocket"].data["depth_mm"] == pytest.approx(25, abs=0.6)
    assert deep["tool_radius"].severity == 1                                      # square corners only
    slot = run(block_with_pocket(25, 20, 0.8, 20, 5), "cnc")
    assert any(f.check == "tool_radius" and f.severity == 3 for f in slot.findings)
    assert slot.metrics["setups"] == ["+Z"]
    hidden = checks(run(sealed(), "cnc"))
    assert hidden["tool_reach"].severity == 3


def test_cli_process_option(tmp_path):
    stl = tmp_path / "tray.stl"
    trimesh.util.concatenate(tray(window=True)).export(stl)
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)  # empty: the built-in config files are used
    out = CliRunner().invoke(app, ["design", str(stl), "--process", "injection", "--material", "PP",
                                   "-n", "2", "-o", str(tmp_path / "r"), "-c", str(cfg)])
    assert out.exit_code == 1, out.output  # the window needs a side action: something to fix first
    assert "Injection moulding, pull Z, PP" in out.output and "side action" in out.output
    assert "check printed" not in out.output and (tmp_path / "r" / "index.html").is_file()
