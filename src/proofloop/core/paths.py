"""Where ProofLoop keeps things: everything in one folder, ``~/.proofloop`` (PROOFLOOP_HOME moves it).

    settings.json    what you set in the window: printers, notifications, loop-file defaults, costs,
                     filament, phone access
    config/          optional YAML for advanced options; a file here replaces the built-in one of
                     the same name (see src/proofloop/data), and settings.yaml is merged over it
    printers.db      print history and events from the printer monitor
    results.db       design checks and what your prints taught
    profiles/ ...    hardware profiles, loop files, reports, check jobs
    proofloop.log    the background service's log
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[1] / "data"  # the built-in config, shipped with ProofLoop
PRINTERS_DB = "printers.db"
RESULTS_DB = "results.db"


def home() -> Path:
    """ProofLoop's folder (made if needed). Read each time, so PROOFLOOP_HOME set later still counts."""
    path = Path(os.environ.get("PROOFLOOP_HOME") or Path.home() / ".proofloop")
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    """Your own config folder (it may be empty: the built-in files are used then)."""
    return home() / "config"


def config_file(folder: Path | str | None, name: str) -> Path:
    """``name`` from ``folder`` (default: your config folder) if it is there, else the built-in one."""
    own = Path(folder) / name if folder else config_dir() / name
    return own if own.is_file() else DEFAULTS / name
