import math
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from synthux.cli import app
from synthux.core.config import ConfigError
from synthux.physical.gcode.checks import parse_duration
from synthux.physical.gcode.parse import parse_stream, read_gcode
from synthux.physical.gcode.pipeline import check_gcode

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "config"
EX = REPO / "examples" / "gcode"
HEAD = ["; printer_model = Bambu Lab P2S", "; nozzle_diameter = 0.4", "G90", "M83", "M140 S{bed}", "M104 S{noz}",
        "G28", "M190 S{bed}", "M109 S{noz}"]


def _write(tmp_path, lines, name="t.gcode", **fmt):
    path = tmp_path / name
    path.write_text("\n".join(x.format(**fmt) for x in HEAD + lines) + "\nM104 S0\nM140 S0\n", encoding="utf-8")
    return path


def _islands(filament="PETG", retract=True, layers=12):
    """Two 20 mm squares 40 mm apart; travel between them every layer."""
    out = [f"; filament_type = {filament}"]
    for i in range(layers):
        z = 0.2 * (i + 1)
        out += ["; CHANGE_LAYER", "M106 P1 S128" if i == 2 else "; -"]
        for cx in (60, 120):
            if retract:
                out.append("G1 E-0.8 F1800")
            out.append(f"G1 X{cx} Y100 Z{z:.2f} F30000")
            if retract:
                out.append("G1 E0.8 F1800")
            out += [f"G1 X{cx + 20} Y100 E0.7 F6000", f"G1 X{cx + 20} Y120 E0.7", f"G1 X{cx} Y120 E0.7",
                    f"G1 X{cx} Y100 E0.7"]
    return out


def _checks(result, min_severity=2):
    return {f["check"] for f in result["findings"] if f["severity"] >= min_severity}


def test_clean_example_passes_and_every_planted_flaw_is_found(tmp_path):
    good = check_gcode(CFG, EX / "bracket_good.gcode", out=tmp_path / "good")
    assert good["printer"]["name"] == "bambu_p2s" and good["printer_source"] == "from the file"
    assert good["material"]["name"] == "PLA" and not _checks(good)
    bad = check_gcode(CFG, EX / "bracket_flawed.gcode", out=tmp_path / "bad")
    assert _checks(bad) == {"bounds", "first_layer", "layer_height", "flow", "cooling", "retraction", "end"}
    assert bad["findings"][0]["severity"] == 4 and bad["findings"][0]["image"] == "plate.png"
    assert (tmp_path / "bad" / "index.html").is_file() and (tmp_path / "bad" / "flow.png").is_file()


def test_bambu_sliced_3mf_reads_the_plate_and_its_slice_info(tmp_path):
    r = check_gcode(CFG, EX / "bracket_good.gcode.3mf", out=tmp_path / "r")
    assert r["plate"] == 1 and r["plates"] == [1] and not _checks(r)
    with pytest.raises(ConfigError, match="plates"):
        read_gcode(EX / "bracket_good.gcode.3mf", plate=3)
    model_only = tmp_path / "model.3mf"
    with zipfile.ZipFile(model_only, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(ConfigError, match="no sliced plates"):
        read_gcode(model_only)


def test_parser_handles_arcs_relative_and_absolute_e_and_resets():
    trace = parse_stream(iter(["G90", "M82", "G92 E0", "G1 X10 Y0 Z0.2 F600", "G1 X20 E1", "G92 E0", "G1 X30 E1",
                               "M83", "G2 X40 Y10 I0 J10 E2", "G1 E-0.5", "G92 X0 Y0", "G1 X5 E0.5"]))
    ext = trace.de > 0
    assert trace.de[ext].sum() == pytest.approx(1 + 1 + 2 + 0.5)
    arc = trace.length[(trace.x0 >= 30) & (trace.de > 0)].sum()
    assert arc == pytest.approx(math.pi * 10 / 2, rel=0.01)             # quarter circle of radius 10
    assert trace.x0[-1] == 0 and trace.x1[-1] == 5                       # G92 moved the origin


def test_layers_without_markers_come_from_extrusion_height(tmp_path):
    lines = []
    for i in range(6):
        z = 0.2 * (i + 1)
        lines += [f"G1 X10 Y10 Z{z + 0.4:.2f} F9000", f"G1 Z{z:.2f}",     # z-hop travel, then down
                  "G1 X40 Y10 E1.5 F3000", "G1 X40 Y40 E1.5", "M106 S255"]
    r = check_gcode(CFG, _write(tmp_path, ["; filament_type = PLA"] + lines, noz=215, bed=60), out=tmp_path / "o")
    assert r["metrics"]["layers"] == 6 and r["metrics"]["first_layer_mm"] == pytest.approx(0.2)


def test_travel_over_open_space_needs_retraction_but_infill_hops_do_not(tmp_path):
    strings = check_gcode(CFG, _write(tmp_path, _islands(retract=False), "a.gcode", noz=240, bed=70), out=tmp_path / "a")
    f = next(f for f in strings["findings"] if f["check"] == "retraction")
    assert "open space" in f["title"] and "24 travel moves on 12 layer(s)" in f["detail"]   # 23 hops + the first approach
    clean = check_gcode(CFG, _write(tmp_path, _islands(retract=True), "b.gcode", noz=240, bed=70), out=tmp_path / "b")
    assert "retraction" not in _checks(clean)


def test_temperatures_filament_and_printer_mismatches(tmp_path):
    cold = check_gcode(CFG, _write(tmp_path, _islands(), "c.gcode", noz=200, bed=70), out=tmp_path / "c")
    assert any(f["check"] == "temperature" and "too cold" in f["title"] for f in cold["findings"])
    frozen = check_gcode(CFG, _write(tmp_path, _islands(), "f.gcode", noz=150, bed=70), out=tmp_path / "f")
    assert frozen["findings"][0]["title"] == "Extrudes with the nozzle cold"
    thick = check_gcode(CFG, _write(tmp_path, ["; filament_diameter = 2.85"] + _islands(), "d.gcode", noz=240, bed=70),
                        out=tmp_path / "d")
    assert thick["findings"][0]["severity"] == 4 and "2.85" in thick["findings"][0]["title"]
    other = check_gcode(CFG, _write(tmp_path, _islands(), "e.gcode", noz=240, bed=70), printer="bambu_h2d",
                        out=tmp_path / "e")
    assert "printer" in _checks(other)


def test_durations_from_each_slicer():
    assert parse_duration("1h 2m 3s") == 3723
    assert parse_duration("2d 1h") == 176400
    assert parse_duration("3456") == 3456
    assert parse_duration(None) is None


def test_cli_gcode_and_check_routes_gcode(tmp_path):
    runner = CliRunner()
    out = runner.invoke(app, ["physical", "gcode", str(EX / "bracket_flawed.gcode"), "-o", str(tmp_path / "g"),
                              "-c", str(CFG)])
    assert out.exit_code == 0, out.output
    assert "will fail" in out.output and "Prints outside the build plate" in out.output
    out = runner.invoke(app, ["physical", "check", str(EX / "bracket_good.gcode"), "-o", str(tmp_path / "h"),
                              "-c", str(CFG)])
    assert out.exit_code == 0 and "checking it as G-code" in out.output and "no problems" in out.output
