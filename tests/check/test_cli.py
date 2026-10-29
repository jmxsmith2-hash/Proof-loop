from pathlib import Path

from typer.testing import CliRunner

from proofloop.check.cli import app

REPO = Path(__file__).resolve().parents[2]
runner = CliRunner()


def _config(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)  # empty: the built-in config files are used
    return cfg


def test_product_review(tmp_path):
    out = runner.invoke(app, ["review", str(REPO / "examples" / "physical" / "trailflask.yaml"), "-n", "3",
                              "-o", str(tmp_path / "phys"), "-c", str(_config(tmp_path))])
    assert out.exit_code == 0 and "PREDICTION, NOT A TEST" in out.output
    assert (tmp_path / "phys" / "index.html").is_file()


def test_new_design_asks_a_few_questions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stl = REPO / "examples" / "cad" / "phone_stand_v1.stl"
    out = runner.invoke(app, ["new", "-c", str(_config(tmp_path))], input=f"{stl}\nPhone stand\nHolds a phone\n\nPLA\nn\n\n")
    assert out.exit_code == 0, out.output
    design = (tmp_path / "designs" / "phone_stand.yaml").read_text(encoding="utf-8")
    assert "name: Phone stand" in design and "proofloop check design" in out.output
