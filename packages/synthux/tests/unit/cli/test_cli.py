from pathlib import Path

from typer.testing import CliRunner

from synthux.cli import app

REPO = Path(__file__).resolve().parents[3]
runner = CliRunner()


def _config(tmp_path):
    cfg = tmp_path / "config"
    (cfg / "calibration").mkdir(parents=True)
    for name in ("settings.example.yaml", "models.yaml", "scales.yaml"):
        (cfg / name).write_text((REPO / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (cfg / "calibration" / "default.yaml").write_text(
        (REPO / "config" / "calibration" / "default.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return cfg


def test_check_and_personas(tmp_path):
    cfg = _config(tmp_path)
    assert "settings OK" in runner.invoke(app, ["check", "-c", str(cfg)]).output
    out = runner.invoke(app, ["personas", "generate", "-n", "4", "-s", str(REPO / "examples" / "survey.csv"), "-c", str(cfg)])
    assert out.exit_code == 0 and out.output.count("per_") == 4
    listing = runner.invoke(app, ["personas", "list", "-c", str(cfg)])
    assert "skill=" in listing.output


def test_run_refuses_urls_off_the_allowlist(tmp_path):
    cfg = _config(tmp_path)
    study = tmp_path / "s.yaml"
    study.write_text("name: s\ntarget: {url: 'https://www.example.com'}\ntasks: [{id: a, goal: g}]\n", encoding="utf-8")
    out = runner.invoke(app, ["run", str(study), "-c", str(cfg)])
    assert out.exit_code == 3 and "Refusing to run" in out.output


def test_dry_run_shows_free_estimate(tmp_path):
    cfg = _config(tmp_path)
    out = runner.invoke(app, ["run", str(REPO / "studies" / "demo.yaml"), "--dry-run", "-c", str(cfg)])
    assert out.exit_code == 0 and "$0.0000" in out.output


def test_calibrate_import_and_physical_review(tmp_path):
    cfg = _config(tmp_path)
    out = runner.invoke(app, ["calibrate", "import", "--json", str(REPO / "examples" / "real_users" / "demo_study.json"),
                              "-c", str(cfg)])
    assert out.exit_code == 0 and "imported real study real_" in out.output
    out = runner.invoke(app, ["physical", "review", str(REPO / "examples" / "physical" / "trailflask.yaml"), "-n", "3",
                              "-o", str(tmp_path / "phys"), "-c", str(cfg)])
    assert out.exit_code == 0 and "PREDICTION, NOT A TEST" in out.output
    assert (tmp_path / "phys" / "index.html").is_file()
