"""Versions for the JSON ProofLoop keeps (settings, the queue, orders), like ``migrations/`` for the databases.

Each of these files carries a ``schema_version``. A file without one is version 0, the format from
before versions. Loading turns an older file into the current format in memory (``upgrade``); saving
writes the current version. The first save over an older file keeps a copy beside it
(``settings.json.v0.bak``). A file from a *newer* ProofLoop is read as far as it can be, but never
written over (``check_write``).

To change a format: add one function under ``@step(kind, N)`` that turns version N into N+1. It
takes the loaded JSON and returns it in the new format; the version number is set for you.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from proofloop.core.logging import get_logger

log = get_logger(__name__)

KINDS = ("settings", "queue", "orders")
KEY = "schema_version"

# Each kind's file as it sits in ProofLoop's folder and in a backup.
FILES = {"settings": "settings.json", "queue": "queue/queue.json", "orders": "orders.json"}

_ACTIONS = {"settings": "changing settings", "queue": "changing the queue", "orders": "changing orders"}
_STEPS: dict[str, list[Callable[[Any], Any]]] = {k: [] for k in KINDS}
_warned: set[str] = set()


class NewerVersionError(ValueError):
    """A file was written by a newer ProofLoop than this one. The message says what to do."""


def step(kind: str, from_version: int):
    """Register the function that turns a ``kind`` file of ``from_version`` into the next version."""

    def add(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        if from_version != len(_STEPS[kind]):
            raise ValueError(f"{kind}: the next step is from version {len(_STEPS[kind])}, not {from_version}")
        _STEPS[kind].append(fn)
        return fn

    return add


def current(kind: str) -> int:
    """The version this ProofLoop writes."""
    return len(_STEPS[kind])


def version_of(doc: Any, label: str = "this file") -> int:
    """The version a loaded file says it is: 0 without one (a list is always version 0)."""
    if not isinstance(doc, dict) or KEY not in doc:
        return 0
    v = doc[KEY]
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"{label} has a version number ProofLoop can't read")
    return v


def _newer(label: str, found: int, known: int, action: str) -> NewerVersionError:
    return NewerVersionError(
        f"{label} was written by a newer ProofLoop (its format {found}, this one knows {known}); "
        f"update ProofLoop (proofloop update) before {action}")


def upgrade(kind: str, doc: Any, label: str, *, newer_ok: bool = False, action: str | None = None) -> tuple[Any, int]:
    """The loaded JSON in the current format, and the version it had.

    A newer file raises ``NewerVersionError``, or with ``newer_ok`` comes back as it is (the caller
    reads what it understands and must not write it back).
    """
    found, known = version_of(doc, label), current(kind)
    if found > known:
        if not newer_ok:
            raise _newer(label, found, known, action or _ACTIONS[kind])
        if label not in _warned:
            _warned.add(label)
            log.warning("%s", _newer(label, found, known, _ACTIONS[kind]))
        return doc, found
    for n in range(found, known):
        doc = _STEPS[kind][n](doc)
    if known and isinstance(doc, dict):
        doc[KEY] = known
    return doc, found


def stamp(kind: str, doc: dict[str, Any]) -> dict[str, Any]:
    """``doc`` with the current version first, for writing."""
    return {KEY: current(kind), **{k: v for k, v in doc.items() if k != KEY}}


def write_json(path: Path, doc: Any, *, private: bool = False) -> None:
    """Write ``doc`` whole or not at all: a temp file in the same folder, then ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600 if private else 0o644)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def check_write(kind: str, path: Path, label: str | None = None) -> None:
    """Call before saving over ``path``: refuses a newer file, keeps a one-time copy of an older one."""
    label = label or path.name
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # missing or damaged: replaced, as it always was
    found = version_of(doc, label)
    known = current(kind)
    if found > known:
        raise _newer(label, found, known, _ACTIONS[kind])
    if found < known:
        copy = path.with_name(f"{path.name}.v{found}.bak")
        if not copy.exists():
            shutil.copy2(path, copy)


def restore_member(name: str, raw: bytes) -> bytes:
    """A file from a backup, in the current format. Other files come back as they are.

    Raises ``ValueError`` for a file from a newer ProofLoop, with a message to show the user.
    """
    kind = next((k for k, f in FILES.items() if PurePosixPath(name) == PurePosixPath(f)), None)
    if kind is None:
        return raw
    doc, found = upgrade(kind, _loads(raw, f"the backup's {name}"), f"The backup's {name}", action="restoring it")
    if found == current(kind):
        return raw
    return json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")


def restore_settings(data: Any) -> Any:
    """The ``settings`` of a backup's ``backup.json``, in the current format, without its version."""
    if not isinstance(data, dict):
        return data
    doc, _ = upgrade("settings", dict(data), "The backup's settings", action="restoring it")
    doc.pop(KEY, None)
    return doc


def _loads(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as e:
        raise ValueError(f"{label} is damaged") from e


@step("settings", 0)
def _settings_v1(doc: Any) -> Any:
    return doc  # same fields; the file now says its version


@step("queue", 0)
def _queue_v1(doc: Any) -> Any:
    return {"runs": doc if isinstance(doc, list) else []}  # was a bare list of runs


@step("orders", 0)
def _orders_v1(doc: Any) -> Any:
    return {"orders": doc if isinstance(doc, list) else []}  # was a bare list of orders
