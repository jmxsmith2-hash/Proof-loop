"""Your verdicts on findings: marking one "not a problem" teaches that check about your setup.

A check you have marked a false alarm at least twice for a printer and material, more often than
you confirmed it, counts one level lower there from then on (four times: only a note). Every
finding it changes says so, with the count, and "it was right" undoes it again.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
MIN_WRONG = 2


def path() -> Path:
    """``feedback.json`` in ProofLoop's folder: what you marked as a false alarm, for every check."""
    from proofloop.core.paths import home

    return home() / "feedback.json"


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record(path: Path, check: str, printer: str, material: str, verdict: str, title: str = "",
           file: str = "") -> dict[str, int]:
    """``verdict`` is "wrong" (a false alarm) or "right". Returns the counts for this check now."""
    if verdict not in ("wrong", "right"):
        raise ValueError("verdict must be 'wrong' or 'right'")
    path = Path(path)
    with _lock:
        rows = _load(path)
        rows.append({"check": check, "printer": printer, "material": material, "verdict": verdict,
                     "title": title[:200], "file": file[:200], "ts": time.time()})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows[-5000:], indent=1), encoding="utf-8")
        tmp.replace(path)
    return counts(path, check, printer, material)


def counts(path: Path, check: str, printer: str, material: str) -> dict[str, int]:
    rows = [r for r in _load(path) if (r["check"], r["printer"], r["material"]) == (check, printer, material)]
    return {"wrong": sum(r["verdict"] == "wrong" for r in rows), "right": sum(r["verdict"] == "right" for r in rows)}


def adjust(findings: list[Any], printer: str, material: str, path: Path) -> int:
    """Lower the findings you keep marking as false alarms on this printer and material. Returns how many."""
    rows = _load(path)
    if not rows:
        return 0
    tally: dict[str, list[int]] = {}
    for r in rows:
        if r.get("printer") == printer and r.get("material") == material:
            t = tally.setdefault(r["check"], [0, 0])
            t[0 if r["verdict"] == "wrong" else 1] += 1
    changed = 0
    for f in findings:
        wrong, right = tally.get(f.check, (0, 0))
        if wrong < MIN_WRONG or wrong <= right or f.severity <= 1 or f.check == "loop_file":
            # (the loop-file safety checks are never lowered)
            continue
        new = 1 if wrong >= 4 else f.severity - 1
        f.detail = (f"{f.detail} (You marked this check a false alarm {wrong} time{'s' if wrong != 1 else ''} on "
                    f"{printer} with {material}, so it counts as "
                    f"{'a note' if new == 1 else 'one level lower'} here.)").strip()
        f.severity = new
        changed += 1
    return changed
