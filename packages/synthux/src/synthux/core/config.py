"""Settings loading: YAML files plus environment variables.

Secrets are never stored in YAML. A value such as ``${DEMO_PASSWORD}`` or
``${DEMO_PASSWORD:-fallback}`` is resolved from the environment (or a local
``.env`` file) when the settings are loaded.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from synthux.core.logging import get_logger

log = get_logger(__name__)

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
ROLES = ("actor", "persona", "judge", "grouper", "reporter")
ENGINES = ("heuristic", "ollama", "anthropic", "fake", "hybrid")
MODEL_ENGINES = ("ollama", "anthropic", "fake")


class ConfigError(ValueError):
    """Raised when settings are missing or invalid."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AllowedHost(StrictModel):
    host: str
    ports: list[int] = Field(default_factory=list)  # empty = any port
    sandbox: bool = False
    note: str = ""


class SafetyConfig(StrictModel):
    allowlist: list[AllowedHost] = Field(default_factory=list)
    blocked_request_patterns: list[str] = Field(default_factory=list)
    payment_patterns: list[str] = Field(default_factory=list)
    email_patterns: list[str] = Field(default_factory=list)
    destructive_patterns: list[str] = Field(default_factory=list)


class HybridConfig(StrictModel):
    """`actor: hybrid`: the free rule engine acts, and a model is asked only on hard screens."""
    engine: str = "anthropic"                     # model engine for the hard screens (anthropic or ollama)
    max_calls_per_session: int = Field(6, ge=0, le=100)

    @field_validator("engine")
    @classmethod
    def _model_engine(cls, value: str) -> str:
        if value not in MODEL_ENGINES:
            raise ValueError(f"hybrid.engine must be one of {MODEL_ENGINES}")
        return value


class EngineConfig(StrictModel):
    default: str = "heuristic"
    roles: dict[str, str] = Field(default_factory=dict)
    hybrid: HybridConfig = Field(default_factory=HybridConfig)
    model_image_width: int = Field(1024, ge=320, le=2000)  # screenshots wider than this are scaled down for models
    batch: bool = False          # Claude via the Batches API for the roles below: half price, answers in minutes
    batch_roles: list[str] = Field(default_factory=lambda: ["judge", "persona", "grouper"])

    @field_validator("default")
    @classmethod
    def _known_engine(cls, value: str) -> str:
        if value not in ENGINES or value == "hybrid":
            raise ValueError(f"unknown default engine {value!r}; choose one of "
                             f"{[e for e in ENGINES if e != 'hybrid']} (hybrid is for the actor role only)")
        return value

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, value: dict[str, str]) -> dict[str, str]:
        for role, engine in value.items():
            if role not in ROLES:
                raise ValueError(f"unknown role {role!r}; roles are {ROLES}")
            if engine not in ENGINES:
                raise ValueError(f"unknown engine {engine!r} for role {role!r}")
            if engine == "hybrid" and role != "actor":
                raise ValueError("hybrid is only for the actor role (the per-step browser behaviour)")
        return value

    @field_validator("batch_roles")
    @classmethod
    def _no_batch_actor(cls, value: list[str]) -> list[str]:
        if "actor" in value:
            raise ValueError("the actor (per-step persona) cannot use batches: each step waits for its answer")
        return value

    def engine_for(self, role: str) -> str:
        return self.roles.get(role, self.default)


class RunConfig(StrictModel):
    repeats: int = Field(3, ge=1, le=20)
    promote_min: int = Field(2, ge=1)
    concurrency: int | Literal["auto"] = "auto"  # "auto" = 2 per CPU core, 2..8

    @field_validator("concurrency")
    @classmethod
    def _workers(cls, v: int | str) -> int:
        if v == "auto":
            return max(2, min(8, 2 * (os.cpu_count() or 1)))
        if not 1 <= int(v) <= 32:
            raise ValueError("concurrency must be 1..32 or 'auto'")
        return int(v)
    max_cost_usd: float = Field(5.0, ge=0)
    seconds_per_step: float = Field(6.0, gt=0)


class NoiseConfig(StrictModel):
    enabled: bool = True
    misclick_rate_by_skill: dict[int, float] = Field(default_factory=dict)
    typo_rate_by_skill: dict[int, float] = Field(default_factory=dict)
    misclick_sigma_px: float = 18.0


class BrowserConfig(StrictModel):
    headless: bool = True
    executable_path: str | None = None
    slow_load_seconds: float = 3.0
    quiet_exit_seconds: float = 1.5   # stop waiting this long after a no-effect action once nothing loads (0 = off)


class DeviceConfig(StrictModel):
    width: int
    height: int
    mobile: bool = False
    scale: float = 1.0


class NetworkConfig(StrictModel):
    latency_ms: int = 0
    download_kbps: int = 0  # 0 = unthrottled
    upload_kbps: int = 0


class ModelProvider(StrictModel):
    roles: dict[str, str] = Field(default_factory=dict)
    prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    effort: dict[str, str] = Field(default_factory=dict)   # role -> low | medium | high | xhigh | max
    base_url: str | None = None


class Settings(StrictModel):
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    engine: EngineConfig = Field(default_factory=EngineConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    noise: NoiseConfig = Field(default_factory=NoiseConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    devices: dict[str, DeviceConfig] = Field(default_factory=dict)
    networks: dict[str, NetworkConfig] = Field(default_factory=dict)
    calibration_profile: str = "default"
    models: dict[str, ModelProvider] = Field(default_factory=dict)
    config_dir: Path = Path("config")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "synthux.sqlite"

    def model_for(self, role: str) -> tuple[str, str]:
        """Return (engine, model id) for a role."""
        engine = self.engine.engine_for(role)
        if engine == "hybrid":
            engine = self.engine.hybrid.engine
        provider = self.models.get(engine)
        model = provider.roles.get(role, "") if provider else ""
        return engine, model or engine

    def device(self, name: str) -> DeviceConfig:
        if name not in self.devices:
            raise ConfigError(f"unknown device {name!r}; known: {sorted(self.devices)}")
        return self.devices[name]

    def network(self, name: str) -> NetworkConfig:
        if name not in self.networks:
            raise ConfigError(f"unknown network {name!r}; known: {sorted(self.networks)}")
        return self.networks[name]


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
    """Locate the ``config`` directory: $SYNTHUX_CONFIG_DIR, then cwd upwards."""
    env_dir = os.environ.get("SYNTHUX_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / "config"
        if (candidate / "settings.yaml").is_file() or (candidate / "settings.example.yaml").is_file():
            return candidate
    return here / "config"


def load_settings(config_dir: Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    """Load settings.yaml (falling back to settings.example.yaml) and models.yaml."""
    config_dir = Path(config_dir) if config_dir else find_config_dir()
    load_dotenv(config_dir.parent / ".env")
    settings_file = config_dir / "settings.yaml"
    if not settings_file.is_file():
        settings_file = config_dir / "settings.example.yaml"
    raw = interpolate_env(_read_yaml(settings_file))
    raw["models"] = interpolate_env(_read_yaml(config_dir / "models.yaml"))
    raw["config_dir"] = str(config_dir)
    for key, value in (overrides or {}).items():
        raw[key] = value
    try:
        settings = Settings.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError, kept readable for the CLI
        raise ConfigError(f"invalid settings in {settings_file}: {exc}") from exc
    if not settings.data_dir.is_absolute():
        settings.data_dir = (config_dir.parent / settings.data_dir).resolve()
    if settings.run.promote_min > settings.run.repeats:
        raise ConfigError("run.promote_min cannot be larger than run.repeats")
    log.debug("settings loaded from %s", settings_file)
    return settings


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Read a YAML file with env interpolation (used for studies and profiles)."""
    return interpolate_env(_read_yaml(Path(path)))
