"""Bambu Lab printer identification.

``printer_model_id`` is written by Bambu Studio into every sliced plate's
``slice_info.config``. This table is copied from the ``model_id`` field of each
``resources/profiles/BBL/machine/<printer>.json`` in the BambuStudio repository
(see docs/sources.md). Bed size and height are NOT stored here: they are read
from each sliced file's own ``printable_area`` / ``printable_height``.
"""

from __future__ import annotations

MODEL_IDS: dict[str, str] = {
    "BL-P001": "X1 Carbon",
    "BL-P002": "X1",
    "C13": "X1E",
    "C11": "P1P",
    "C12": "P1S",
    "N1": "A1 mini",
    "N2S": "A1",
    "O1D": "H2D",
    "O1E": "H2D Pro",
    "O1S": "H2S",
    "N7": "P2S",
    "O1C2": "H2C",
    "N6": "X2D",
    "N9": "A2L",
}

P2S = "N7"


def model_name(model_id: str) -> str:
    return MODEL_IDS.get(model_id, f"unknown printer ({model_id or 'no id in file'})")


def model_id_for(printer_model: str) -> str:
    """The model id for a printer as Bambu Studio and Orca name it ("Bambu Lab P2S"), or ""."""
    name = (printer_model or "").strip()
    if name.lower().startswith("bambu lab "):
        name = name[len("bambu lab "):]
    for model_id, model in MODEL_IDS.items():
        if model.lower() == name.lower():
            return model_id
    return ""
