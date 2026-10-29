"""Settings loading: YAML files plus environment variables.

Secrets are never stored in YAML. A value such as ``${API_KEY}`` or
``${API_KEY:-fallback}`` is resolved from the environment (or a local
``.env`` file) when the settings are loaded.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from proofloop.core import paths
from proofloop.core.logging import get_logger

log = get_logger(__name__)

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
ROLES = ("persona",)  # persona writing and product predictions
ENGINES = ("heuristic", "ollama", "anthropic", "fake")


class ConfigError(ValueError):
    """Raised when settings are missing or invalid."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EngineConfig(StrictModel):
    default: str = "heuristic"
    roles: dict[str, str] = Field(default_factory=dict)
    batch: bool = False          # Claude via the Batches API for the roles below: half price, answers in minutes
    batch_roles: list[str] = Field(default_factory=list)

    @field_validator("default")
    @classmethod
    def _known_engine(cls, value: str) -> str:
        if value not in ENGINES:
            raise ValueError(f"unknown default engine {value!r}; choose one of {list(ENGINES)}")
        return value

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, value: dict[str, str]) -> dict[str, str]:
        for role, engine in value.items():
            if role not in ROLES:
                raise ValueError(f"unknown role {role!r}; roles are {ROLES}")
            if engine not in ENGINES:
                raise ValueError(f"unknown engine {engine!r} for role {role!r}")
        return value

    def engine_for(self, role: str) -> str:
        return self.roles.get(role, self.default)


class ModelProvider(StrictModel):
    roles: dict[str, str] = Field(default_factory=dict)
    prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    effort: dict[str, str] = Field(default_factory=dict)   # role -> low | medium | high | xhigh | max
    base_url: str | None = None


class Settings(StrictModel):
    data_dir: Path | None = None  # default: ProofLoop's folder, ~/.proofloop
    log_level: str = "INFO"
    engine: EngineConfig = Field(default_factory=EngineConfig)
    models: dict[str, ModelProvider] = Field(default_factory=dict)
    config_dir: Path = Path("config")

    @property
    def db_path(self) -> Path:
        return self.data_dir / paths.RESULTS_DB

    def model_for(self, role: str) -> tuple[str, str]:
        """Return (engine, model id) for a role."""
        engine = self.engine.engine_for(role)
        provider = self.models.get(engine)
        model = provider.roles.get(role, "") if provider else ""
        return engine, model or engine


# --------------------------------------------------------------------------- env


def load_dotenv(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from a .env file without overriding the real environment."""
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    if loaded:
        log.debug("loaded %d variables from %s", len(loaded), path)
    return loaded


def interpolate_env(value: Any, *, strict: bool = True) -> Any:
    """Replace ${VAR} / ${VAR:-default} recursively in strings, lists and dicts."""
    if isinstance(value, dict):
        return {k: interpolate_env(v, strict=strict) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v, strict=strict) for v in value]
    if not isinstance(value, str):
        return value

    def _sub(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        if strict:
            raise ConfigError(f"environment variable {name} is not set")
        return ""

    return ENV_PATTERN.sub(_sub, value)


def secret(value: str | None) -> SecretStr | None:
    return SecretStr(value) if value is not None else None


# ------------------------------------------------------------------------- loading


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def find_config_dir(start: Path | None = None) -> Path:
    """Your config folder, ``~/.proofloop/config`` (PROOFLOOP_HOME moves it). It may be empty."""
    return paths.config_dir()


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """``over`` on top of ``base``, section by section (a list replaces the list it overrides)."""
    out = dict(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load_settings(config_dir: Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    """The built-in settings.yaml with yours (``config/settings.yaml``) on top, and models.yaml."""
    config_dir = Path(config_dir) if config_dir else find_config_dir()
    load_dotenv(config_dir.parent / ".env")
    raw = interpolate_env(_read_yaml(paths.DEFAULTS / "settings.yaml"))
    settings_file = config_dir / "settings.yaml"
    if config_dir.resolve() != paths.DEFAULTS:
        raw = _merge(raw, interpolate_env(_read_yaml(settings_file)))
    raw["models"] = interpolate_env(_read_yaml(paths.config_file(config_dir, "models.yaml")))
    raw["config_dir"] = str(config_dir)
    for key, value in (overrides or {}).items():
        raw[key] = value
    try:
        settings = Settings.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError, kept readable for the CLI
        raise ConfigError(f"invalid settings in {settings_file}: {exc}") from exc
    if settings.data_dir is None:
        settings.data_dir = paths.home()
    elif not settings.data_dir.is_absolute():
        settings.data_dir = (config_dir.parent / settings.data_dir).resolve()
    log.debug("settings loaded from %s", settings_file)
    return settings


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Read a YAML file with env interpolation (used for studies and profiles)."""
    return interpolate_env(_read_yaml(Path(path)))
