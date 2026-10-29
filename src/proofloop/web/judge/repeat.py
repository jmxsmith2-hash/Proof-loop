"""Repetition: an issue is promoted only if it recurs across the N runs of a persona-task pair."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Recurrence:
    key: tuple[str, str, str]                     # (task, category, screen)
    runs_with: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))   # persona -> repeats
    runs_total: dict[str, int] = field(default_factory=dict)
    sessions_with: int = 0
    sessions_total: int = 0

    @property
    def best_persona_runs(self) -> int:
        return max((len(v) for v in self.runs_with.values()), default=0)

    @property
    def frequency(self) -> float:
        return self.sessions_with / self.sessions_total if self.sessions_total else 0.0

    def promoted(self, promote_min: int) -> bool:
        return self.best_persona_runs >= promote_min

    def describe(self) -> str:
        parts = [f"{len(r)}/{self.runs_total.get(p, 0)} runs" for p, r in sorted(self.runs_with.items())]
        return f"seen in {self.sessions_with}/{self.sessions_total} sessions ({', '.join(parts)} per persona)"


def group_key(issue: dict[str, Any]) -> tuple[str, str, str]:
    return issue["task_id"], issue["category"], issue["screen_id"] or ""


def recurrence(issues: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> dict[tuple[str, str, str], Recurrence]:
    """Count, per issue group, how many repeats of each persona-task pair showed it."""
    by_id = {s["id"]: s for s in sessions}
    repeats: dict[tuple[str, str], set[int]] = defaultdict(set)
    sessions_per_task: dict[str, int] = defaultdict(int)
    for s in sessions:
        repeats[(s["persona_id"], s["task_id"])].add(s["repeat_index"])
        sessions_per_task[s["task_id"]] += 1
    groups: dict[tuple[str, str, str], Recurrence] = {}
    seen_sessions: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for issue in issues:
        key = group_key(issue)
        rec = groups.setdefault(key, Recurrence(key))
        session = by_id[issue["session_id"]]
        rec.runs_with[issue["persona_id"]].add(session["repeat_index"])
        rec.runs_total[issue["persona_id"]] = len(repeats[(issue["persona_id"], issue["task_id"])])
        seen_sessions[key].add(issue["session_id"])
    for key, rec in groups.items():
        rec.sessions_with = len(seen_sessions[key])
        rec.sessions_total = sessions_per_task[key[0]]
    return groups
