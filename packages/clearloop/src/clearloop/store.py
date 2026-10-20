"""SQLite log of monitor events and plate checks."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    printer TEXT,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    printer TEXT NOT NULL,
    reason TEXT NOT NULL,
    clear INTEGER NOT NULL,
    changed_fraction REAL,
    resumed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_checks_printer ON checks(printer, ts);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def add_event(
        self, printer: str | None, level: str, kind: str, message: str, data: dict | None = None
    ) -> dict:
        ts = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (ts, printer, level, kind, message, data) VALUES (?,?,?,?,?,?)",
                (ts, printer, level, kind, message, json.dumps(data) if data else None),
            )
        return {
            "id": cur.lastrowid,
            "ts": ts,
            "printer": printer,
            "level": level,
            "kind": kind,
            "message": message,
        }

    def events(self, limit: int = 100, printer: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM events", []
        if printer:
            q += " WHERE printer=?"
            args.append(printer)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = [dict(r) for r in self._db.execute(q, args)]
        for r in rows:
            r["data"] = json.loads(r["data"]) if r["data"] else None
        return rows

    def add_check(
        self, printer: str, reason: str, clear: bool, changed_fraction: float | None, resumed: bool
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO checks (ts, printer, reason, clear, changed_fraction, resumed) VALUES (?,?,?,?,?,?)",
                (time.time(), printer, reason, int(clear), changed_fraction, int(resumed)),
            )

    def stats(self, printer: str, since: float = 0) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS checks, SUM(clear) AS clear, SUM(resumed) AS resumed FROM checks "
                "WHERE printer=? AND ts>=? AND reason='loop end'",
                (printer, since),
            ).fetchone()
        return {k: (row[k] or 0) for k in ("checks", "clear", "resumed")}
