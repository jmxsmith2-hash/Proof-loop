"""What a persona remembers during a session and between visits."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionMemory:
    # long-term (carried to return visits)
    facts: list[str] = field(default_factory=list)
    known_formats: dict[str, str] = field(default_factory=dict)          # field kind -> value that worked
    known_targets: dict[str, dict[str, Any]] = field(default_factory=dict)  # screen signature -> where to click
    label_meanings: dict[str, str] = field(default_factory=dict)        # ambiguous label -> field kind that worked
    visits: int = 0
    # short-term (this session)
    screen_sig: str = ""
    typed: dict[str, str] = field(default_factory=dict)        # field label -> intended value on current form
    typed_kinds: dict[str, str] = field(default_factory=dict)  # field label -> field kind used
    form_attempts: int = 0
    variant_index: dict[str, int] = field(default_factory=dict)  # field label -> which alternative to try
    vary_cursor: int = 0
    dead_targets: set[tuple[str, str]] = field(default_factory=set)
    missed_once: set[tuple[str, str]] = field(default_factory=set)   # clicked once with no effect
    clicks: list[dict[str, Any]] = field(default_factory=list)
    choices_done: set[str] = field(default_factory=set)
    scrolls: int = 0
    at_bottom: bool = False
    careful: bool = False
    scrolled_up: bool = False
    retries_same: int = 0
    waits: int = 0
    last_error: str = ""
    last_action_kind: str = ""
    last_scroll_dy: int = 0
    dismissed_popups: set[str] = field(default_factory=set)   # closed and confirmed gone
    popup_pending: str = ""                                    # closed last step, not yet confirmed
    popup_stuck: set[str] = field(default_factory=set)         # closing it did nothing (covered?)
    ticked: set[str] = field(default_factory=set)
    nav_tried: dict[str, set[str]] = field(default_factory=dict)
    target_pending: tuple[str, dict[str, Any]] | None = None     # clicked; kept if it worked
    followed: set[tuple[str, str]] = field(default_factory=set)   # (screen, link) already followed from there
    nav_depth: int = 0
    explored: set[str] = field(default_factory=set)       # screens where I already tried menu/search early
    menus_opened: set[str] = field(default_factory=set)   # screens whose menu icon I already opened
    searches: int = 0                                      # site searches tried
    pending_enter: bool = False                            # typed a search; Enter comes next
    backs: int = 0

    def remember(self, fact: str) -> None:
        if fact not in self.facts:
            self.facts.append(fact)

    def commit_target(self) -> None:
        """The last click worked (the next screen moved on, or the task was done): remember it for next time."""
        if self.target_pending:
            self.known_targets[self.target_pending[0]] = self.target_pending[1]
        self.target_pending = None

    def new_screen(self, sig: str) -> None:
        self.screen_sig = sig
        self.scrolls = 0
        self.at_bottom = False
        self.careful = False
        self.scrolled_up = False
        self.retries_same = 0
        self.waits = 0

    def degrade(self, keep: float, rng: random.Random) -> list[str]:
        """Interruption: forget part of what happened. Returns the facts that were forgotten."""
        forgotten = [f for f in self.facts if rng.random() > keep]
        self.facts = [f for f in self.facts if f not in forgotten]
        if rng.random() > keep:
            self.typed.clear()
            self.typed_kinds.clear()
        if rng.random() > keep:
            self.variant_index.clear()
        self.dead_targets = {t for t in self.dead_targets if rng.random() <= keep}
        self.new_screen("")
        return forgotten

    def long_term(self) -> dict[str, Any]:
        return {"facts": self.facts[-12:], "known_formats": self.known_formats,
                "known_targets": self.known_targets, "label_meanings": self.label_meanings,
                "visits": self.visits + 1}

    @classmethod
    def from_long_term(cls, data: dict[str, Any] | None) -> "SessionMemory":
        if not data:
            return cls()
        return cls(facts=list(data.get("facts", [])), known_formats=dict(data.get("known_formats", {})),
                   known_targets=dict(data.get("known_targets", {})),
                   label_meanings=dict(data.get("label_meanings", {})), visits=int(data.get("visits", 0)))
