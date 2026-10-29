"""SQLite storage: a thin wrapper with numbered migrations and JSON columns."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from proofloop.core.logging import get_logger

log = get_logger(__name__)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _encode(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool):
        return int(value)
    return value


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key.endswith("_json") and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        out[key] = value
    return out


class Database:
    """One SQLite file. Safe to share across asyncio tasks in one thread or across threads."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self.migrate()

    # ------------------------------------------------------------------ schema
    def migrate(self) -> list[str]:
        """Apply any migration files that have not run yet. Returns the applied names."""
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)"
            )
            done = {r[0] for r in self._conn.execute("SELECT name FROM schema_migrations")}
            applied = []
            folder = resources.files("proofloop.core") / "migrations"
            for entry in sorted(folder.iterdir(), key=lambda p: p.name):
                if not entry.name.endswith(".sql") or entry.name in done:
                    continue
                log.info("applying migration %s", entry.name)
                self._conn.executescript("BEGIN;" + entry.read_text(encoding="utf-8") + ";COMMIT;")
                self._conn.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)", (entry.name, utcnow())
                )
                applied.append(entry.name)
            return applied

    # ------------------------------------------------------------------ access
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(_encode(p) for p in params))

    def insert(self, table: str, row: dict[str, Any], *, replace: bool = False) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        cur = self.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", row.values())
        return int(cur.lastrowid or 0)

    def update(self, table: str, key: str, key_value: Any, changes: dict[str, Any]) -> None:
        sets = ", ".join(f"{c} = ?" for c in changes)
        self.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", [*changes.values(), key_value])

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [_decode_row(r) for r in self.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        row = self.execute(sql, params).fetchone()
        return _decode_row(row) if row else None

    def tables(self) -> set[str]:
        return {r["name"] for r in self.all("SELECT name FROM sqlite_master WHERE type='table'")}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
