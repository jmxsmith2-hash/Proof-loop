"""Farm configuration (YAML)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .gcode.routines import LoopSettings
from .gcode.sweep import SweepSettings
from .profiles import PrinterProfile, printer_profile


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SweepConfig(_Model):
    lane_spacing: float = 25.0
    clearance: float = 6.0
    push_ratio: float = 0.3
    max_push_z: float = 12.0
    top_margin: float = 1.0
    travel_margin: float = 5.0
    final_wipe: bool = False
    wipe_spacing: float = 30.0


class LoopConfig(_Model):
    cooldown_mode: Literal["temperature", "time"] = "temperature"
    release_temp: float | None = None
    cooldown_time_s: int = 600
    natural_cooldown_min: int | None = None
    min_cooldown_s: int = 30
    door_opener: bool = False
    bend_window_s: int = 25
    retract_wait_s: int = 4
    marker_base: int = 9000
    marker_hold_s: int = 2
    bed_temp: float | None = None
    sweep: SweepConfig = Field(default_factory=SweepConfig)

    def merged(self, override: dict | None) -> LoopConfig:
        if not override:
            return self
        data = self.model_dump()
        sweep = {**data["sweep"], **override.get("sweep", {})}
        data.update({k: v for k, v in override.items() if k != "sweep"})
        data["sweep"] = sweep
        return LoopConfig.model_validate(data)

    def to_settings(self, eject_mode: str, custom_gcode: str = "") -> LoopSettings:
        d = self.model_dump()
        d.pop("bed_temp")
        sweep = SweepSettings(**d.pop("sweep"))
        return LoopSettings(eject_mode=eject_mode, custom_eject_gcode=custom_gcode, sweep=sweep, **d)


class EjectorConfig(_Model):
    kind: Literal["gcode", "custom", "clearboard", "http", "webhook", "mqtt"] = "gcode"
    url: str | None = None
    eject_url: str | None = None
    prepare_url: str | None = None
    door_url: str | None = None
    status_url: str | None = None
    token: str | None = None
    timeout_s: float = 30.0
    gcode: str | None = None
    magazine_size: int | None = None
    mqtt_host: str | None = None
    mqtt_port: int = 1883
    command_topic: str | None = None
    ack_topic: str | None = None
    username: str | None = None
    password: str | None = None
    quiet_profile: str = "quiet"


class VerifyConfig(_Model):
    enabled: bool = True
    roi: tuple[float, float, float, float] = (0.15, 0.25, 0.85, 0.95)  # x0, y0, x1, y1 as image fractions
    pixel_threshold: int = 38
    max_changed_fraction: float = 0.004
    min_blob_px: int = 30
    settle_s: float = 3.0


class PlateConfig(_Model):
    type: str = "textured_pei"
    rated_cycles: int = 1500


class PrinterConfig(_Model):
    name: str
    model: str
    host: str
    serial: str
    access_code: str
    camera: Literal["auto", "tcp_jpeg", "rtsps", "none"] = "auto"
    snapshot_url: str | None = None
    start_url_template: str = "file:///sdcard/{name}"
    use_ams: bool = True
    profile_overrides: dict = Field(default_factory=dict)
    ejector: EjectorConfig = Field(default_factory=EjectorConfig)
    loop: dict = Field(default_factory=dict)
    verify: VerifyConfig | None = None
    plate: PlateConfig = Field(default_factory=PlateConfig)
    max_retries: int = 1
    enabled: bool = True

    def profile(self) -> PrinterProfile:
        return printer_profile(self.model, **self.profile_overrides)

    def camera_kind(self) -> str:
        return self.profile().camera if self.camera == "auto" else self.camera


class NotifierConfig(_Model):
    kind: Literal["ntfy", "discord", "webhook"]
    url: str
    min_level: Literal["info", "warning", "error"] = "warning"
    token: str | None = None


class QuietHours(_Model):
    start: str  # "22:00"
    end: str  # "07:00"
    mode: Literal["quiet", "hold"] = "quiet"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        h, m = v.split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError("use HH:MM")
        return v


class CostConfig(_Model):
    currency: str = "USD"
    filament_per_kg: float = 20.0
    power_kw: float = 0.15
    kwh_price: float = 0.30
    machine_per_hour: float = 0.0
    plate_cost: float = 0.0  # spread over rated_cycles


class SkuConfig(_Model):
    sku: str
    file: str
    parts_per_plate: int = 1
    models: list[str] = Field(default_factory=list)


class ApiToken(_Model):
    token: str
    role: Literal["viewer", "operator", "admin"] = "operator"
    name: str = ""


class FarmConfig(_Model):
    name: str = "ClearLoop farm"
    secret: str | None = None
    data_dir: str = "./data"
    api_tokens: list[ApiToken] = Field(default_factory=list)
    shopify_webhook_secret: str | None = None


class Config(_Model):
    farm: FarmConfig = Field(default_factory=FarmConfig)
    defaults: LoopConfig = Field(default_factory=LoopConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    quiet_hours: QuietHours | None = None
    notifications: list[NotifierConfig] = Field(default_factory=list)
    costs: CostConfig = Field(default_factory=CostConfig)
    printers: list[PrinterConfig] = Field(default_factory=list)
    skus: list[SkuConfig] = Field(default_factory=list)

    def printer(self, name: str) -> PrinterConfig:
        for p in self.printers:
            if p.name == name:
                return p
        raise KeyError(f"no printer named {name!r} in config")

    def loop_for(self, p: PrinterConfig) -> LoopConfig:
        return self.defaults.merged(p.loop)

    def verify_for(self, p: PrinterConfig) -> VerifyConfig:
        return p.verify or self.verify

    @property
    def data_path(self) -> Path:
        return Path(self.farm.data_dir)


def load_config(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text()) or {}
    cfg = Config.model_validate(data)
    names = [p.name for p in cfg.printers]
    if len(names) != len(set(names)):
        raise ValueError("printer names must be unique")
    for p in cfg.printers:
        p.profile()  # fail early on an unknown model
    return cfg
