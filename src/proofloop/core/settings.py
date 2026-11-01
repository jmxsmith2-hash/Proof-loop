"""Your settings, as ``settings.json`` in ProofLoop's folder: everything you set in the window."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from proofloop.core import printers, schema
from proofloop.monitor.costing import CostSettings
from proofloop.monitor.filament import FilamentSettings
from proofloop.monitor.maintenance import MaintenanceSettings


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
    sounds: Literal["every", "last", "off"] = "every"  # the printer's start and finish tunes in a loop file
    load_line_every_loop: bool = False  # Bambu's nozzle load line: first loop only unless this is on
    level_once: bool = False  # bed leveling, vibration sweep and flow calibration on loop 1 only
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
    hands_off: bool = False  # send the next queued job by itself once the plate checks empty (monitor/handsoff.py)
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

    @field_validator("serial")
    @classmethod
    def _serial(cls, v: str) -> str:
        # The printer reports on device/<serial>/report with its serial in capitals, and MQTT topics are
        # case-sensitive: typed in lower case, the printer connects but ProofLoop never hears from it.
        return re.sub(r"\s+", "", v).upper()

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return v.strip()


class Notifier(_Model):
    """One place notices go. ntfy, Discord and webhooks use ``url``; email uses the ``smtp_*`` and ``mail_*``
    fields; Telegram uses ``bot_token`` and ``chat_id``. ``token``, ``smtp_password`` and ``bot_token`` are
    secrets: they are saved here and never sent back to a page (see ``core/notify.py``)."""

    kind: Literal["ntfy", "discord", "webhook", "email", "telegram"]
    url: str = ""
    min_level: Literal["info", "warning", "error"] = "warning"
    token: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(None, ge=1, le=65535)  # blank: 587 (starttls), 465 (ssl) or 25 (none)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_user: str | None = None
    smtp_password: str | None = None
    mail_from: str | None = None
    mail_to: list[str] = Field(default_factory=list)
    bot_token: str | None = None
    chat_id: str | None = None

    @field_validator("url", mode="before")
    @classmethod
    def _url_text(cls, v):
        return v.strip() if isinstance(v, str) else ("" if v is None else v)

    @field_validator("smtp_host", "smtp_user", "mail_from", "bot_token", "chat_id", mode="before")
    @classmethod
    def _trim(cls, v):
        if isinstance(v, int) and not isinstance(v, bool):
            v = str(v)  # a Telegram chat id may arrive as a number
        return (v.strip() or None) if isinstance(v, str) else v

    @field_validator("token", "smtp_password", mode="before")
    @classmethod
    def _blank_secret(cls, v):
        return v or None  # an empty box means "none" (or "keep the saved one": see the notifications route)

    @field_validator("mail_to", mode="before")
    @classmethod
    def _addresses(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = re.split(r"[,;\s]+", v)
        return [a.strip() for a in v if isinstance(a, str) and a.strip()]

    @model_validator(mode="after")
    def _complete(self):
        if self.kind in ("ntfy", "discord", "webhook"):
            if not self.url:
                raise ValueError("enter the URL to send to")
        elif self.kind == "email":
            if not self.smtp_host:
                raise ValueError("enter the mail server (SMTP host)")
            if not self.mail_from and self.smtp_user and "@" in self.smtp_user:
                self.mail_from = self.smtp_user
            if not self.mail_from or "@" not in self.mail_from:
                raise ValueError("enter the address the email comes from")
            if not self.mail_to:
                raise ValueError("enter at least one address to send to")
            if any("@" not in a for a in self.mail_to):
                raise ValueError("each address to send to needs an @")
        elif self.kind == "telegram":
            if not self.bot_token or not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", self.bot_token):
                raise ValueError("enter the bot token from @BotFather (it has a colon in it)")
            if not self.chat_id:
                raise ValueError("enter the chat id to send to")
        return self


class MqttExport(_Model):
    """Optional, off by default: publish each printer's state to your own MQTT broker for Home Assistant."""

    enabled: bool = False
    host: str = ""
    port: int | None = Field(None, ge=1, le=65535)  # blank: 1883, or 8883 with TLS
    username: str | None = None
    password: str | None = None  # a secret: saved here, never sent back to a page
    tls: bool = False
    tls_verify: bool = True  # off for a broker with a self-signed certificate
    base_topic: str = "proofloop"
    discovery_prefix: str = "homeassistant"

    @field_validator("host", "username", "base_topic", "discovery_prefix", mode="before")
    @classmethod
    def _trim(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("username", "password", mode="before")
    @classmethod
    def _blank(cls, v):
        return v or None

    @field_validator("base_topic", "discovery_prefix")
    @classmethod
    def _topic(cls, v: str) -> str:
        v = v.strip("/")
        if not v or len(v) > 100 or any(c in v for c in "+#") or "//" in v:
            raise ValueError("use a plain topic like proofloop (no +, # or empty parts)")
        return v

    @model_validator(mode="after")
    def _complete(self):
        if self.enabled and not self.host:
            raise ValueError("enter the broker's address first")
        return self

    @property
    def port_or_default(self) -> int:
        return self.port or (8883 if self.tls else 1883)


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


class Business(_Model):
    """Who an invoice is from. Blank until you fill it in: ProofLoop never makes these up."""

    name: str = Field("", max_length=200)
    address: str = Field("", max_length=1000)
    email: str = Field("", max_length=200)
    tax_id: str = Field("", max_length=100)
    prefix: str = Field("INV-", max_length=20)  # in front of the invoice number
    next_number: int = Field(1, ge=1, le=99999999)  # the number the next new invoice gets
    footer: str = Field("", max_length=1000)  # payment details, a thank-you

    @field_validator("name", "address", "email", "tax_id", "prefix", "footer")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()


class CameraSettings(_Model):
    """What ProofLoop does with the printers' cameras while a print runs."""

    look_every_s: int = 30  # the failure watch looks at the print every this many seconds (0: by its layers)
    look_every_pct: int = 10  # by layers: every this many % of its layers (0: only at 50% and 80%)
    timelapse: bool = False  # keep a picture every few layers and make an animation when the print ends
    timelapse_every_layers: int = Field(5, ge=1, le=500)
    timelapse_keep: int = Field(5, ge=1, le=50)  # timelapses kept for each printer
    proof_photos: bool = True  # a picture of each loop's finished plate before the sweep, for the order's gallery
    proof_keep: int = Field(500, ge=10, le=5000)  # proof photos kept, over all printers (about 50 kB each)

    @model_validator(mode="before")
    @classmethod
    def _by_layers_before(cls, data):
        # Settings from before looks by time: if you had chosen how often by layers, keep looking that way.
        if isinstance(data, dict) and "look_every_s" not in data and data.get("look_every_pct", 10) != 10:
            data = {**data, "look_every_s": 0}
        return data

    @field_validator("look_every_s")
    @classmethod
    def _seconds(cls, v: int) -> int:
        if v != 0 and not 20 <= v <= 600:
            raise ValueError("look every 20 to 600 seconds, or 0 to look by the layers")
        return v

    @field_validator("look_every_pct")
    @classmethod
    def _pct(cls, v: int) -> int:
        if v != 0 and not 5 <= v <= 25:
            raise ValueError("look every 5 to 25 percent of the layers, or 0 to look only at 50% and 80%")
        return v


class SliceUsual(_Model):
    """The presets you last sliced with in ProofLoop: what the watch folder slices new models with."""

    machine: str = ""
    process: str = ""
    filament: str = ""
    plate: str = ""
    support: bool | None = None
    orient: bool = False
    copies: int = Field(1, ge=1, le=100)
    fill: bool = False


class WatchFolder(_Model):
    """A folder ProofLoop slices new models from, so they are waiting in Make a loop file."""

    on: bool = False
    path: str = Field("", max_length=1000)  # "": Downloads/ProofLoop
    printer: str = ""  # slice for this printer's nozzle, nozzle flow and AMS ("": the first one)


class Settings(_Model):
    defaults: Defaults = Field(default_factory=Defaults)
    slice_usual: SliceUsual = Field(default_factory=SliceUsual)
    watch_folder: WatchFolder = Field(default_factory=WatchFolder)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    costs: CostSettings = Field(default_factory=CostSettings)
    filament: FilamentSettings = Field(default_factory=FilamentSettings)
    printers: list[PrinterConfig] = Field(default_factory=list)
    notifications: list[Notifier] = Field(default_factory=list)
    app_token: str | None = None  # optional: require this token for the app's API
    send_to_printer: bool = False  # let ProofLoop copy a loop file to a printer and start it
    send_confirm: bool = True  # ask "Start ... on ...?" before each send; off: the Send button alone starts it
    failure_watch: Literal["off", "alert", "pause"] = "alert"  # the plate still looks empty part-way through
    camera: CameraSettings = Field(default_factory=CameraSettings)
    summary_at: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # "07:30": a daily summary
    mqtt_export: MqttExport = Field(default_factory=MqttExport)  # Home Assistant / MQTT export (off by default)
    maintenance: MaintenanceSettings = Field(default_factory=MaintenanceSettings)  # tasks, intervals, reminders
    business: Business = Field(default_factory=Business)  # who invoices are from


class SettingsStore:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.path = self.dir / "settings.json"
        self._lock = threading.Lock()

    def load(self) -> Settings:
        """Your settings. A file from a newer ProofLoop is read as far as it can be, but not saved over."""
        if not self.path.exists():
            return Settings()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw, _ = schema.upgrade("settings", raw, self.path.name, newer_ok=True)
            raw.pop(schema.KEY, None)
        return Settings.model_validate(raw)

    def save(self, settings: Settings) -> None:
        with self._lock:
            schema.check_write("settings", self.path)  # holds printer access codes, so the file is private
            doc = schema.stamp("settings", json.loads(settings.model_dump_json()))
            schema.write_json(self.path, doc, private=True)
