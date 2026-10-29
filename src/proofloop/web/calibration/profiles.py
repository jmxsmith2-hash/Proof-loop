"""Calibration profiles: the simulator parameters that real-user data tunes.

Profiles are versioned. The base profile is a YAML file in config/calibration/;
applied adjustments are stored as new versions in the database so every run
records exactly which parameters produced it.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from proofloop.core.config import ConfigError, Settings, load_yaml_file
from proofloop.core.db import Database, utcnow
from proofloop.core.logging import get_logger
from proofloop.core.paths import config_file

log = get_logger(__name__)


class PatienceParams(BaseModel):
    base_steps: int = 14
    steps_per_patience_point: int = 5
    min_steps: int = 6
    retries_by_patience: dict[int, int] = Field(default_factory=lambda: {1: 1, 2: 2, 3: 3, 4: 4, 5: 6})
    seconds_by_patience: dict[int, float] = Field(default_factory=lambda: {1: 90, 2: 150, 3: 240, 4: 360, 5: 600})
    time_pressure_factor: float = 0.6


class FrustrationParams(BaseModel):
    threshold_by_patience: dict[int, float] = Field(default_factory=lambda: {1: 35, 2: 50, 3: 65, 4: 80, 5: 95})
    weights: dict[str, float] = Field(default_factory=dict)
    progress_relief: float = 4.0
    mood_multiplier: dict[str, float] = Field(default_factory=lambda: {"calm": 1.0, "distracted": 1.15, "stressed": 1.3})


class ElicitationParams(BaseModel):
    min_complaints_per_screen: int = 1


class JudgeParams(BaseModel):
    max_rounds: int = 2
    replay_hash_threshold: int = 12


class NoveltyParams(BaseModel):
    max_familiarity: int = 2
    min_share_low_familiarity: float = 0.75


class PerceptionParams(BaseModel):
    """Contrast a persona needs to notice text at a glance, and when looking carefully (heuristic engine)."""
    glance_contrast: dict[str, float] = Field(default_factory=lambda: {"normal": 2.2, "color_blind": 2.3, "low_vision": 3.6})
    careful_contrast: dict[str, float] = Field(default_factory=lambda: {"normal": 1.3, "color_blind": 1.35, "low_vision": 2.4})


class CalibrationProfile(BaseModel):
    version: str = "default"
    perception: PerceptionParams = Field(default_factory=PerceptionParams)
    patience: PatienceParams = Field(default_factory=PatienceParams)
    frustration: FrustrationParams = Field(default_factory=FrustrationParams)
    elicitation: ElicitationParams = Field(default_factory=ElicitationParams)
    judge: JudgeParams = Field(default_factory=JudgeParams)
    novelty: NoveltyParams = Field(default_factory=NoveltyParams)
    prompt_notes: list[str] = Field(default_factory=list)

    def weight(self, name: str) -> float:
        return float(self.frustration.weights.get(name, 0.0))

    def fingerprint(self) -> str:
        data = self.model_dump(exclude={"version"})
        return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:10]


def load_profile(settings: Settings, db: Database | None = None, name: str | None = None) -> CalibrationProfile:
    """Load a stored version (from the DB) or a YAML profile from config/calibration/ (or the built-in one)."""
    name = name or settings.calibration_profile
    if db is not None:
        row = db.one("SELECT params_json FROM calibration_profiles WHERE version = ?", [name])
        if row:
            return CalibrationProfile.model_validate(row["params_json"])
    path = config_file(settings.config_dir, f"calibration/{name}.yaml")
    if not path.is_file():
        raise ConfigError(f"calibration profile {name!r} not found (looked in the database and {path})")
    profile = CalibrationProfile.model_validate(load_yaml_file(path))
    if db is not None:
        store_profile(db, profile, parent=None, note=f"loaded from {path.name}")
    return profile


def store_profile(db: Database, profile: CalibrationProfile, parent: str | None, note: str) -> None:
    if db.one("SELECT version FROM calibration_profiles WHERE version = ?", [profile.version]):
        return
    db.insert("calibration_profiles", {"version": profile.version, "parent": parent, "created_at": utcnow(),
                                       "params_json": profile.model_dump(), "note": note})


def apply_changes(profile: CalibrationProfile, changes: list[dict[str, Any]], version: str) -> CalibrationProfile:
    """Return a new profile with dotted-path changes applied, e.g. {"path": "frustration.weights.no_change", "value": 8}."""
    data = copy.deepcopy(profile.model_dump())
    for change in changes:
        if change["path"] == "prompt_notes":
            data["prompt_notes"] = list(dict.fromkeys([*data["prompt_notes"], change["value"]]))
            continue
        node = data
        parts = change["path"].split(".")
        for part in parts[:-1]:
            key = int(part) if part.isdigit() and int(part) in node else part
            node = node[key]
        last = parts[-1]
        key = int(last) if last.isdigit() else last
        node[key] = change["value"]
    data["version"] = version
    return CalibrationProfile.model_validate(data)
