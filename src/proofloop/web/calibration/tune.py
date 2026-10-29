"""Turn disagreements with real users into concrete, reviewable parameter changes."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from proofloop.core.db import Database
from proofloop.web.calibration.match import Match, infer_category
from proofloop.web.calibration.profiles import CalibrationProfile, apply_changes, store_profile

# Which frustration weight drives each issue category (for missed or over-reported issues).
CATEGORY_WEIGHT = {
    "unexplained_error": "unexplained_error", "data_lost_on_error": "data_lost", "slow_response": "slow_load",
    "no_loading_feedback": "no_loading_feedback", "ambiguous_label": "confusion",
    "unclear_input_format": "confusion", "hidden_control": "cannot_find", "dead_click": "no_change",
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def suggest(profile: CalibrationProfile, completion: dict[str, Any], matches: list[Match],
            real_only: list[dict[str, Any]], synthetic_only: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}

    def propose(path: str, current: float, new: float, reason: str, kind: str = "number") -> None:
        if path in changes:
            changes[path]["to"] = round(changes[path]["to"] + (new - current), 2)
            changes[path]["reasons"].append(reason)
        elif round(new, 2) != round(current, 2):
            changes[path] = {"path": path, "from": current, "to": round(new, 2), "reasons": [reason], "kind": kind}

    # 1. Completion rates: personas too persistent or too fragile overall.
    bias = completion.get("bias")
    if bias is not None and abs(bias) > 0.1:
        factor = _clamp(1 - bias * 0.8, 0.6, 1.5)
        direction = "finish more often" if bias > 0 else "give up more often"
        for level, threshold in profile.frustration.threshold_by_patience.items():
            propose(f"frustration.threshold_by_patience.{level}", threshold, round(threshold * factor),
                    f"synthetic personas {direction} than real users (completion bias {bias:+.0%})")
        steps = profile.patience.base_steps
        propose("patience.base_steps", steps, max(profile.patience.min_steps, round(steps * factor)),
                f"step budget scaled with the completion bias ({bias:+.0%})")

    # 2. Real users hit problems the personas missed: make the matching signal count for more.
    missed = Counter(r.get("category") or infer_category(f"{r['title']} {r.get('description', '')}") for r in real_only)
    for category, n in missed.items():
        if category == "hidden_control":
            glance = profile.perception.glance_contrast["normal"]
            propose("perception.glance_contrast.normal", glance, round(glance + 0.3 * n, 2),
                    f"real users missed controls the personas found ({n} issue(s)); personas should notice less at a glance")
        weight = CATEGORY_WEIGHT.get(category or "")
        if weight:
            current = profile.weight(weight)
            propose(f"frustration.weights.{weight}", current, current + 2 * n,
                    f"{n} real-user issue(s) of type '{category}' were not found by personas")
    for r in real_only:
        propose_note = f"Real users struggled with: {r['title']}" + (f" on {r['screen']}" if r.get("screen") else "")
        changes.setdefault(f"prompt_notes::{propose_note}", {"path": "prompt_notes", "from": None, "to": propose_note,
                                                             "reasons": ["model-backed personas only"],
                                                             "kind": "note"})

    # 3. Personas reported problems real users did not: soften those signals (but say they may be real).
    over = Counter(s["category"] for s in synthetic_only)
    for category, n in over.items():
        weight = CATEGORY_WEIGHT.get(category)
        if weight:
            current = profile.weight(weight)
            propose(f"frustration.weights.{weight}", current, max(0.0, current - 1.5 * n),
                    f"{n} synthetic finding(s) of type '{category}' were not seen by real users "
                    "(check them before applying: the real study may have missed them)")

    out = []
    for n, change in enumerate(changes.values(), 1):
        reasons = change.pop("reasons")
        out.append({"id": n, **change, "reason": "; ".join(reasons)})
    return out


def next_version(db: Database, base: str) -> str:
    root = base.split("-cal")[0]
    existing = db.all("SELECT version FROM calibration_profiles WHERE version LIKE ?", [f"{root}-cal%"])
    numbers = [int(v["version"].rsplit("-cal", 1)[1]) for v in existing if v["version"].rsplit("-cal", 1)[1].isdigit()]
    return f"{root}-cal{max(numbers, default=0) + 1}"


def apply_suggestions(db: Database, profile: CalibrationProfile, suggestions: list[dict[str, Any]],
                      config_dir: Path | None = None, only: list[int] | None = None, note: str = "") -> CalibrationProfile:
    chosen = [s for s in suggestions if not only or s["id"] in only]
    version = next_version(db, profile.version)
    changes = [{"path": s["path"], "value": s["to"]} for s in chosen]
    new = apply_changes(profile, changes, version)
    store_profile(db, new, parent=profile.version, note=note or f"{len(chosen)} calibration change(s)")
    if config_dir is not None:
        path = Path(config_dir) / "calibration" / f"{version}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = f"# Calibrated from {profile.version}. Changes:\n" + "".join(
            f"#  - {s['path']}: {s['from']} -> {s['to']} ({s['reason']})\n" for s in chosen)
        path.write_text(header + yaml.safe_dump(new.model_dump(), sort_keys=False), encoding="utf-8")
    return new
