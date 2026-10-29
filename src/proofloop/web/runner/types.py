"""Data passed between the runner, the persona engines and the judge."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ActionKind = Literal["click", "fill", "type", "press", "scroll", "wait", "back", "quit"]
FailureClass = Literal["UX_ISSUE", "AGENT_LIMITATION", "BLOCKED_BY_SAFETY"]
Outcome = Literal["completed", "quit", "budget_exhausted", "agent_failure", "blocked"]


@dataclass
class Action:
    kind: ActionKind
    x: int | None = None
    y: int | None = None
    text: str | None = None          # for fill/type; may contain {{cred:name}} placeholders
    key: str | None = None           # for press
    dy: int = 0                      # for scroll
    target: str = ""                 # what the persona believes it is acting on (visible label)
    field_kind: str | None = None    # for fill: which field the persona thinks this is

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", 0) or k == "kind"}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def describe(self) -> str:
        if self.kind == "click":
            return f"click '{self.target}'" if self.target else f"click at ({self.x}, {self.y})"
        if self.kind == "fill":
            shown = "[secret]" if self.text and "{{cred:" in self.text else self.text
            return f"type '{shown}' into '{self.target}'"
        if self.kind == "scroll":
            return "scroll down" if self.dy > 0 else "scroll up"
        if self.kind == "press":
            return f"press {self.key}"
        return self.kind


@dataclass
class ThinkAloud:
    sees: str
    expects: str
    does: str
    feels: str


@dataclass
class Observation:
    """Something the persona noticed that may point to a UX problem."""
    kind: str                         # e.g. ambiguous_label, data_lost_on_error, vague_error
    detail: str
    region: dict[str, int] | None = None
    severity: int = 2


@dataclass
class Decision:
    action: Action
    think: ThinkAloud
    confusion: float = 0.0            # 0..1, self-reported
    observations: list[Observation] = field(default_factory=list)
    quit_reason: str | None = None
    limitation: str | None = None     # set when the persona cannot perform what it needs (agent limitation)
    dismisses_popup: bool = False     # closing a cookie banner or pop-up (the screen behind it is dimmed/covered)


@dataclass
class Timing:
    first_change_s: float | None      # seconds until the screen first changed (None = never)
    settled_s: float                  # seconds until the screen stopped changing
    intermediate_states: int          # distinct screens seen while waiting (spinners, partial loads)
    major_change_s: float | None = None  # when the last large change happened (new page painted)
    responded: bool = True            # the screen visibly reacted (not just a focus ring while loading)


@dataclass
class Signals:
    error_seen: bool = False
    unexplained_error: bool = False
    error_text: str = ""
    no_change: bool = False
    slow_load: bool = False
    load_seconds: float = 0.0
    no_loading_feedback: bool = False
    repeat_action: bool = False
    backtrack: bool = False
    confusion: float = 0.0
    data_lost: bool = False
    cannot_find: bool = False
    scroll_search: bool = False
    progress: bool = False
    scroll_end: bool = False          # a scroll moved nothing (end of page)
    loading: bool = False             # the browser was still loading after the action
    load_trigger: str = ""            # the control whose click started the load being measured
    error_region: dict | None = None  # where the error message is on screen
    noise_applied: bool = False       # the executed action differed from the intended one

    def active(self) -> list[str]:
        return [k for k, v in asdict(self).items()
                if isinstance(v, bool) and v and k not in ("progress", "noise_applied", "scroll_end", "loading")]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StepRecord:
    idx: int
    screenshot_path: str
    screen_hash: int
    screen_id: str
    url: str
    think: ThinkAloud
    intended: Action
    executed: Action | None
    signals: Signals
    frustration: float
    sim_seconds: float
    observations: list[Observation] = field(default_factory=list)
    screen_id_after: str = ""         # page shown after the action settled
    after_screenshot: str = ""        # saved when the result of the action is itself the evidence


@dataclass
class IssueCandidate:
    category: str
    title: str
    description: str
    severity: int                     # 1 cosmetic, 2 minor, 3 major, 4 blocker
    step_idx: int
    screen_id: str
    region: dict[str, int] | None
    source: Literal["behavior", "elicited"] = "behavior"
    failure_class: FailureClass = "UX_ISSUE"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionResult:
    session_id: str
    persona_id: str
    task_id: str
    repeat_index: int
    visit_index: int
    seed: int
    outcome: Outcome
    quit_reason: str | None
    steps: list[StepRecord]
    issues: list[IssueCandidate]
    sim_seconds: float
    wall_seconds: float
    final_frustration: float
    frustration_timeline: list[float]
    memory: dict[str, Any]
    conditions: dict[str, Any]
    a11y: list[Any] = field(default_factory=list)   # measured accessibility findings (proofloop.web.a11y)
