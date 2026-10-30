from pathlib import Path

import pytest

from proofloop.core.config import (
    ConfigError,
    EngineConfig,
    find_config_dir,
    interpolate_env,
    load_dotenv,
    load_settings,
)

REPO = Path(__file__).resolve().parents[2]


def test_example_settings_load_with_free_defaults():
    s = load_settings(REPO / "src" / "proofloop" / "data")
    assert s.engine.default == "heuristic"
    assert s.model_for("persona") == ("heuristic", "rules")


def test_role_override_routes_to_anthropic_models():
    s = load_settings(REPO / "src" / "proofloop" / "data")
    assert s.model_for("persona")[0] == "heuristic"
    s = load_settings(REPO / "src" / "proofloop" / "data", overrides={"engine": {"roles": {"persona": "anthropic"}}})
    assert s.model_for("persona") == ("anthropic", "claude-haiku-5-5")


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


def test_unknown_keys_rejected(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text("not_a_setting: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg)


def test_your_config_folder_is_in_proofloops_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFLOOP_HOME", str(tmp_path))
    assert find_config_dir() == tmp_path / "config"
    s = load_settings()                                       # nothing there yet: all built-in
    assert s.data_dir == tmp_path and s.db_path == tmp_path / "results.db"
    assert s.engine.default == "heuristic" and s.models       # settings.yaml and models.yaml, built in


def test_your_settings_go_over_the_built_in_ones(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text("engine: {roles: {persona: anthropic}}\n", encoding="utf-8")
    s = load_settings(cfg)
    assert s.engine.roles == {"persona": "anthropic"}         # only what you set changes
    assert s.engine.default == "heuristic" and s.log_level == "INFO"
