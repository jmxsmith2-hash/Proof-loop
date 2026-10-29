"""Your printers, set up once: the ones you added in ClearLoop are synthux's printers too.

ClearLoop keeps each printer's name and Bambu model id (``settings.json`` in its folder). synthux
matches the model to its own printer profiles (by the names Bambu Studio uses, such as "Bambu Lab
P2S"), offers your printers first and checks for the first one unless you pick another.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

def _models() -> dict[str, str]:
    """Bambu printer_model_id -> model name, from ClearLoop (only its printers are listed here anyway)."""
    try:
        from clearloop.printers import MODEL_IDS
    except ImportError:
        return {}
    return MODEL_IDS


def clearloop_printers(clearloop_dir: Path | str | None = None) -> list[dict[str, Any]]:
    from synthux.physical.learn import CLEARLOOP_DIR

    try:
        data = json.loads((Path(clearloop_dir or CLEARLOOP_DIR) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in data.get("printers") or [] if isinstance(p, dict) and p.get("name")]


def profile_for_model(printers_yaml: dict[str, Any], model_id: str) -> str | None:
    """The synthux printer profile for a Bambu model id, from the names in printers.yaml."""
    model = _models().get(model_id or "")
    if not model:
        return None
    wanted = f"Bambu Lab {model}".lower()
    for key, spec in (printers_yaml.get("printers") or {}).items():
        if wanted in [n.lower() for n in (spec or {}).get("names") or []]:
            return key
    return None


def farm_printers(printers_yaml: dict[str, Any], clearloop_dir: Path | str | None = None) -> list[dict[str, str]]:
    """[{"id": synthux profile, "label": "Your name (Bambu Lab P2S)"}] for every ClearLoop printer synthux knows."""
    out = []
    for p in clearloop_printers(clearloop_dir):
        key = profile_for_model(printers_yaml, p.get("model_id", ""))
        if key:
            out.append({"id": key, "label": f"{p['name']} (Bambu Lab {_models()[p['model_id']]})", "name": p["name"]})
    return out


def default_printer(printers_yaml: dict[str, Any], clearloop_dir: Path | str | None = None) -> str | None:
    """Your first ClearLoop printer, else printers.yaml's default_printer."""
    farm = farm_printers(printers_yaml, clearloop_dir)
    return farm[0]["id"] if farm else printers_yaml.get("default_printer")
