"""Import results from real user tests.

Accepted formats (templates in examples/real_users/):

JSON:
  {"name": "...", "tasks": [{"task_id", "participants", "completed", "median_seconds",
                             "quits": [{"screen", "reason", "count"}]}],
   "issues": [{"task_id", "screen", "title", "description", "severity", "participants_affected", "category"}]}

CSV (two files):
  tasks.csv:  task_id, participants, completed, median_seconds, quit_screen, quit_reason, quit_count
              (repeat the task row once per quit point)
  issues.csv: task_id, screen, title, description, severity, participants_affected, category
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from proofloop.core.db import Database, new_id, utcnow
from proofloop.core.logging import get_logger
from proofloop.personas.anonymize import anonymize_text

log = get_logger(__name__)


class ImportError_(ValueError):
    """The real-user file is missing required columns or values."""


def _int(value: Any, field: str, default: int | None = None) -> int:
    if value in (None, ""):
        if default is None:
            raise ImportError_(f"missing required value: {field}")
        return default
    try:
        return int(float(value))
    except ValueError as exc:
        raise ImportError_(f"{field} must be a number, got {value!r}") from exc


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _from_csv(tasks_csv: Path, issues_csv: Path | None) -> dict[str, Any]:
    tasks: dict[str, dict[str, Any]] = {}
    with tasks_csv.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            tid = (row.get("task_id") or "").strip()
            if not tid:
                raise ImportError_("tasks.csv: every row needs task_id")
            task = tasks.setdefault(tid, {"task_id": tid, "participants": _int(row.get("participants"), "participants"),
                                          "completed": _int(row.get("completed"), "completed"),
                                          "median_seconds": _float(row.get("median_seconds")), "quits": []})
            if row.get("quit_screen"):
                task["quits"].append({"screen": row["quit_screen"].strip(), "reason": row.get("quit_reason", ""),
                                      "count": _int(row.get("quit_count"), "quit_count", 1)})
    issues = []
    if issues_csv:
        with issues_csv.open(newline="", encoding="utf-8-sig") as fh:
            issues = [dict(r) for r in csv.DictReader(fh)]
    return {"tasks": list(tasks.values()), "issues": issues}


def load_real_results(json_file: Path | None = None, tasks_csv: Path | None = None,
                      issues_csv: Path | None = None) -> dict[str, Any]:
    if json_file:
        data = json.loads(Path(json_file).read_text(encoding="utf-8"))
    elif tasks_csv:
        data = _from_csv(Path(tasks_csv), Path(issues_csv) if issues_csv else None)
    else:
        raise ImportError_("give a JSON file or a tasks CSV")
    for task in data.get("tasks", []):
        if task["completed"] > task["participants"]:
            raise ImportError_(f"task {task['task_id']}: completed > participants")
    for issue in data.get("issues", []):
        if not issue.get("title"):
            raise ImportError_("every real issue needs a title")
    return data


def import_real_study(db: Database, name: str, data: dict[str, Any], source: str = "") -> str:
    """Store real-user results (anonymized) and return the real study id."""
    study_id = new_id("real")
    db.insert("real_user_studies", {"id": study_id, "name": name or data.get("name", "real study"),
                                    "imported_at": utcnow(), "source_file": source,
                                    "meta_json": {k: v for k, v in data.items() if k not in ("tasks", "issues")}})
    for task in data.get("tasks", []):
        db.insert("real_user_tasks", {
            "study_id": study_id, "task_id": task["task_id"], "participants": _int(task["participants"], "participants"),
            "completed": _int(task["completed"], "completed"), "median_seconds": _float(task.get("median_seconds")),
            "quits_json": [{**q, "reason": anonymize_text(str(q.get("reason", "")))} for q in task.get("quits", [])]})
    for issue in data.get("issues", []):
        db.insert("real_user_issues", {
            "study_id": study_id, "task_id": issue.get("task_id"), "screen": issue.get("screen"),
            "category": issue.get("category") or None, "title": anonymize_text(issue["title"]),
            "description": anonymize_text(issue.get("description", "")),
            "severity": _int(issue.get("severity"), "severity", 2),
            "participants_affected": _int(issue.get("participants_affected"), "participants_affected", 1)})
    log.info("imported real study %s: %d tasks, %d issues", study_id, len(data.get("tasks", [])),
             len(data.get("issues", [])))
    return study_id


def real_study(db: Database, study_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM real_user_studies WHERE id = ?", [study_id])
    if row is None:
        raise KeyError(f"no real study {study_id}")
    tasks = db.all("SELECT * FROM real_user_tasks WHERE study_id = ?", [study_id])
    issues = db.all("SELECT * FROM real_user_issues WHERE study_id = ?", [study_id])
    quits: dict[str, list] = defaultdict(list)
    for t in tasks:
        quits[t["task_id"]] = t["quits_json"]
    return {"study": row, "tasks": tasks, "issues": issues, "quits": dict(quits)}
