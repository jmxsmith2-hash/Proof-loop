"""Your printers, set up once: the printers you add under Printers are the ones designs are checked for.

``settings.json`` keeps each printer's name and Bambu model id. The checks match the model to a
printer profile in printers.yaml (by the names Bambu Studio uses, such as "Bambu Lab P2S"), offer
your printers first and check for the first one unless you pick another.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from proofloop.core.paths import home
from proofloop.core.printers import MODEL_IDS


def _models() -> dict[str, str]:
    """Bambu printer_model_id -> model name (the printers you can add are all listed there)."""
    return MODEL_IDS


def saved_printers(data_dir: Path | str | None = None) -> list[dict[str, Any]]:
    try:
        data = json.loads((Path(data_dir or home()) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in data.get("printers") or [] if isinstance(p, dict) and p.get("name")]


def profile_for_model(printers_yaml: dict[str, Any], model_id: str) -> str | None:
    """The printer profile for a Bambu model id, from the names in printers.yaml."""
    model = _models().get(model_id or "")
    if not model:
        return None
    wanted = f"Bambu Lab {model}".lower()
    for key, spec in (printers_yaml.get("printers") or {}).items():
        if wanted in [n.lower() for n in (spec or {}).get("names") or []]:
            return key
    return None


def farm_printers(printers_yaml: dict[str, Any], data_dir: Path | str | None = None) -> list[dict[str, str]]:
    """[{"id": printer profile, "label": "Your name (Bambu Lab P2S)"}] for each of your printers with a profile."""
    out = []
    for p in saved_printers(data_dir):
        key = profile_for_model(printers_yaml, p.get("model_id", ""))
        if key:
            out.append({"id": key, "label": f"{p['name']} (Bambu Lab {_models()[p['model_id']]})", "name": p["name"]})
    return out


def default_printer(printers_yaml: dict[str, Any], data_dir: Path | str | None = None) -> str | None:
    """Your first printer, else printers.yaml's default_printer."""
    farm = farm_printers(printers_yaml, data_dir)
    return farm[0]["id"] if farm else printers_yaml.get("default_printer")
