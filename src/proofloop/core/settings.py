"""Your settings, as ``settings.json`` in ProofLoop's folder: everything you set in the window."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from proofloop.core import printers
from proofloop.monitor.costing import CostSettings
from proofloop.monitor.filament import FilamentSettings


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Defaults(_Model):
    """Starting values for new loop files. Every one is shown, and can be changed, in the app."""

    profile_id: str = "p2s-stage2-farmboard"
    release_temp: float = Field(30.0, ge=15, le=60)  # FarmLoop's default cooldown target
    floor_z: float = Field(2.0, ge=0.5, le=30)  # calibrate in the Calibrate tab
    floor_calibrated: bool = False
    push_offset: float = Field(10.0, ge=0, le=50)  # FarmLoop: part height minus 10 mm
    natural_cooldown_min: int = Field(0, ge=0, le=120)  # FarmLoop allows 1-120 min
    bender_fitted: bool = True
    cooldown_fan: Literal["profile", "exhaust", "off"] = "profile"
    wait_for_monitor: bool = False
    # Pause before loop 1's sweep: "auto" = until a loop has been swept on one of your printers
    pause_first_sweep: Literal["auto", "on", "off"] = "auto"
    loops: int = Field(1, ge=1, le=50)


class VerifyConfig(_Model):
    roi: tuple[float, float, float, float] = (0.15, 0.25, 0.85, 0.95)  # plate area in the image (fractions)
    pixel_threshold: int = 38
    max_changed_fraction: float = 0.004
    min_blob_px: int = 30
    settle_s: float = 3.0


class PrinterConfig(_Model):
    name: str
    host: str
    serial: str
    access_code: str
    model_id: str
    snapshot_url: str | None = None
    auto_resume: bool = True  # resume a loop-end pause when the plate checks out empty
    verify: VerifyConfig = Field(default_factory=VerifyConfig)

    @field_validator("model_id")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in printers.MODEL_IDS:
            raise ValueError(f"unknown printer model id {v!r}")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v or "/" in v:
            raise ValueError("give the printer a short name without '/'")
        return v


class Notifier(_Model):
    kind: Literal["ntfy", "discord", "webhook"]
    url: str
    min_level: Literal["info", "warning", "error"] = "warning"
    token: str | None = None


class QuietHours(_Model):
    """While on, the monitor still checks plates but holds the next loop until the window ends."""

    enabled: bool = False
    start: str = "22:00"
    end: str = "07:00"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        h, _, m = v.partition(":")
        if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError("use HH:MM, e.g. 22:00")
        return f"{int(h):02d}:{int(m):02d}"

    def until(self, now=None) -> str | None:
        """The end time while quiet hours are on, else None."""
        if not self.enabled:
            return None
        from datetime import datetime

        t = (now or datetime.now()).strftime("%H:%M")
        inside = self.start <= t < self.end if self.start <= self.end else (t >= self.start or t < self.end)
        return self.end if inside else None


class Settings(_Model):
    defaults: Defaults = Field(default_factory=Defaults)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    costs: CostSettings = Field(default_factory=CostSettings)
    filament: FilamentSettings = Field(default_factory=FilamentSettings)
    printers: list[PrinterConfig] = Field(default_factory=list)
    notifications: list[Notifier] = Field(default_factory=list)
    app_token: str | None = None  # optional: require this token for the app's API


class SettingsStore:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.path = self.dir / "settings.json"
        self._lock = threading.Lock()

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        return Settings.model_validate(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, settings: Settings) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)  # holds printer access codes
            except OSError:
                pass
