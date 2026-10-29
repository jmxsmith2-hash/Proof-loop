from pathlib import Path

import pytest

from synthux.core.config import (
    ConfigError,
    EngineConfig,
    find_config_dir,
    interpolate_env,
    load_dotenv,
    load_settings,
)

REPO = Path(__file__).resolve().parents[3]


def test_example_settings_load_with_free_defaults():
    s = load_settings(REPO / "config")
    assert s.engine.default == "heuristic"
    assert s.model_for("actor") == ("heuristic", "rules")
    assert s.run.repeats == 3 and s.run.promote_min == 2
    assert s.device("mobile").width == 390


def test_role_override_routes_to_anthropic_models():
    s = load_settings(REPO / "config", overrides={"engine": {"default": "heuristic", "roles": {"judge": "anthropic"}}})
    assert s.model_for("judge") == ("anthropic", "claude-opus-5-5")
    assert s.model_for("actor")[0] == "heuristic"


def test_unknown_engine_rejected():
    with pytest.raises(ValueError):
        EngineConfig(default="gpt")


def test_env_interpolation(monkeypatch):
    monkeypatch.setenv("SX_SECRET", "hunter2")
    assert interpolate_env({"a": ["${SX_SECRET}", "${SX_MISSING:-dflt}"]}) == {"a": ["hunter2", "dflt"]}
    with pytest.raises(ConfigError):
        interpolate_env("${SX_DEFINITELY_MISSING}")


def test_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SX_KEEP", "real")
    monkeypatch.delenv("SX_NEW", raising=False)
    env = tmp_path / ".env"
    env.write_text("SX_KEEP=fake\nexport SX_NEW='new'\n# comment\n", encoding="utf-8")
    loaded = load_dotenv(env)
    import os
    assert os.environ["SX_KEEP"] == "real"
    assert os.environ["SX_NEW"] == "new"
    assert loaded == {"SX_NEW": "new"}
    monkeypatch.delenv("SX_NEW")


def test_promote_min_cannot_exceed_repeats(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text("run: {repeats: 2, promote_min: 3}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg)


def test_unknown_keys_rejected(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text("not_a_setting: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg)


def test_config_found_from_the_combined_repository(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNTHUX_CONFIG_DIR", raising=False)
    cfg = tmp_path / "packages" / "synthux" / "config"
    cfg.mkdir(parents=True)
    (cfg / "settings.example.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    assert find_config_dir(tmp_path) == cfg
    assert find_config_dir(tmp_path / "scripts") == cfg
    assert find_config_dir(cfg.parent / "examples") == cfg


def test_config_found_from_anywhere_for_an_installed_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNTHUX_CONFIG_DIR", raising=False)
    assert find_config_dir(tmp_path) == REPO / "config"  # nothing in tmp_path: synthux's own folder


def test_browser_for_website_tests_downloads_on_first_use(monkeypatch):
    import subprocess

    from synthux.runner import browser

    s = load_settings(REPO / "config")
    monkeypatch.setattr(browser, "find_chromium", lambda settings: None)
    monkeypatch.setattr(browser, "_fallback_chromium", lambda: None)
    ran, stages = [], []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    browser.ensure_chromium(s, stages.append)
    assert ran and ran[0][-3:] == ["playwright", "install", "chromium"] and "once" in stages[0]
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no network"))
    with pytest.raises(ConfigError):
        browser.ensure_chromium(s)
    monkeypatch.setattr(browser, "_fallback_chromium", lambda: "/opt/chromium")
    browser.ensure_chromium(s)  # already there: nothing to do
