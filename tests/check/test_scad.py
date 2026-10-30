"""Parts written as OpenSCAD code are rendered with OpenSCAD, then checked like any other part."""

import json
import sys

from proofloop.check.cad.load import load_model
from proofloop.cli import main

# Stands in for OpenSCAD: `openscad -o OUT.stl IN.scad` writes the cube the .scad file asks for.
FAKE = """
import re, sys, trimesh
out, src = sys.argv[sys.argv.index("-o") + 1], sys.argv[-1]
size = [float(x) for x in re.search(r"cube\\(\\[([^\\]]+)\\]", open(src).read()).group(1).split(",")]
if size[0] <= 0:
    print("ERROR: Parser error in line 1", file=sys.stderr)
    sys.exit(1)
trimesh.creation.box(extents=size).export(out)
"""


def stand_in(tmp_path, monkeypatch):
    exe = tmp_path / "openscad.py"
    exe.write_text(FAKE, encoding="utf-8")
    monkeypatch.setenv("PROOFLOOP_OPENSCAD", str(exe))


def test_an_openscad_part_is_rendered_and_checked(tmp_path, monkeypatch, capsys):
    stand_in(tmp_path, monkeypatch)
    part = tmp_path / "block.scad"
    part.write_text("cube([40, 30, 20]);\n", encoding="utf-8")
    m = load_model(part)
    assert [round(x) for x in m.size] == [40, 30, 20] and m.path == part
    assert main(["check", str(part), "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["kind"] == "design" and d["ready"] and d["size_mm"] == [40.0, 30.0, 20.0]


def test_an_openscad_error_is_said_plainly(tmp_path, monkeypatch, capsys):
    stand_in(tmp_path, monkeypatch)
    part = tmp_path / "broken.scad"
    part.write_text("cube([0, 1, 1]);\n", encoding="utf-8")
    assert main(["check", str(part)]) == 2
    assert "OpenSCAD couldn't render broken.scad: ERROR: Parser error in line 1" in capsys.readouterr().err
    monkeypatch.setenv("PROOFLOOP_OPENSCAD", "")
    monkeypatch.setattr("proofloop.check.cad.load.OPENSCAD_PLACES", ())
    monkeypatch.setenv("PATH", "")
    assert main(["check", str(part)]) == 2
    assert "needs OpenSCAD (openscad.org)" in capsys.readouterr().err


def test_findings_carry_the_numbers_behind_them(capsys):
    from pathlib import Path

    cad = Path(__file__).resolve().parents[2] / "examples" / "cad"
    main(["check", str(cad / "phone_stand_v1.stl"), "--design", str(cad / "phone_stand.yaml"), "--json"])
    d = json.loads(capsys.readouterr().out)
    assert any(f.get("measured") for f in d["findings"])
    assert sys.modules  # the JSON is the whole of stdout
