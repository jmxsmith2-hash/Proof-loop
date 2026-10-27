"""A study: one target, its tasks, which personas to use and under what conditions."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator

from synthux.core.config import ConfigError, Settings, StrictModel, load_yaml_file
from synthux.safety.allowlist import check_url
from synthux.safety.credentials import CredentialVault


class TaskSpec(StrictModel):
    id: str
    goal: str
    start: str = "/"
    success_text: list[str] = Field(default_factory=list)   # visible text that means "done"
    success_url: str | None = None                           # or the address bar contains this
    keywords: list[str] = Field(default_factory=list)        # words a person would look for
    inputs: dict[str, str] = Field(default_factory=dict)     # field kind -> value ({{cred:x}} allowed)
    price_points: list[float] = Field(default_factory=list)  # for forced-choice price questions
    max_steps: int | None = None                             # hard cap regardless of patience


class TargetSpec(StrictModel):
    url: str
    credentials: dict[str, SecretStr] = Field(default_factory=dict)
    brand_mask: list[str] = Field(default_factory=list)      # CSS selectors blurred in screenshots
    framing: Literal["neutral", "competitor"] = "neutral"
    competitor_label: str = "a competing product"


class PersonaSpec(StrictModel):
    count: int = Field(6, ge=1, le=60)
    sources: list[str] = Field(default_factory=list)
    manual: list[dict] = Field(default_factory=list)
    ids: list[str] = Field(default_factory=list)             # reuse stored personas


class Conditions(StrictModel):
    device: str | None = None            # None = the persona's own device
    network: str = "fast"
    interruption_step: int | None = None
    interruption_memory_keep: float = Field(0.4, ge=0, le=1)
    time_pressure: bool | None = None    # None = the persona's own trait


class Study(StrictModel):
    name: str
    target: TargetSpec
    tasks: list[TaskSpec]
    personas: PersonaSpec = Field(default_factory=PersonaSpec)
    conditions: Conditions = Field(default_factory=Conditions)
    visits: int = Field(1, ge=1, le=5)  # >1 = return visits with memory (learnability)
    repeats: int | None = Field(None, ge=1, le=20)
    source_path: str | None = None

    @field_validator("tasks")
    @classmethod
    def _unique_tasks(cls, tasks: list[TaskSpec]) -> list[TaskSpec]:
        ids = [t.id for t in tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        if not tasks:
            raise ValueError("a study needs at least one task")
        return tasks

    def vault(self) -> CredentialVault:
        return CredentialVault(self.target.credentials)

    def task_url(self, task: TaskSpec) -> str:
        if task.start.startswith(("http://", "https://")):
            return task.start
        return self.target.url.rstrip("/") + "/" + task.start.lstrip("/")

    def task(self, task_id: str) -> TaskSpec:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)


def load_study(path: Path | str, settings: Settings) -> Study:
    """Load a study and refuse it if any of its URLs are off the allowlist."""
    path = Path(path)
    raw = load_yaml_file(path)
    if not raw:
        raise ConfigError(f"study file {path} is empty or missing")
    raw["source_path"] = str(path)
    try:
        study = Study.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"invalid study {path}: {exc}") from exc
    check_url(study.target.url, settings.safety.allowlist)
    for task in study.tasks:
        check_url(study.task_url(task), settings.safety.allowlist)
    return study


def is_sandbox(study: Study, settings: Settings) -> bool:
    return check_url(study.target.url, settings.safety.allowlist).sandbox
