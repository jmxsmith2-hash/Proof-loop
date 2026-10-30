"""What every command shares: JSON for programs and assistants, and exit codes that mean the same everywhere.

Exit codes: ``0`` the check passed (nothing major or worse), ``1`` it found something to fix first,
``2`` it couldn't run (a missing file, a bad option, ProofLoop not running when it must be).
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

OK, PROBLEMS, ERROR = 0, 1, 2
BLOCKING = 3  # severity: 1 note, 2 minor, 3 major, 4 will fail


def _default(o: Any) -> Any:
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (set, tuple)):
        return list(o)
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "item"):  # numpy numbers
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    return str(o)


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=_default)


def emit(obj: Any) -> None:
    """Write ``obj`` as JSON on stdout (and nothing else goes there)."""
    sys.stdout.write(dumps(obj) + "\n")
    sys.stdout.flush()


def exit_code(findings: list[dict], key: str = "severity") -> int:
    return PROBLEMS if any((f.get(key) or 0) >= BLOCKING for f in findings) else OK
