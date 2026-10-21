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
CREATE TABLE IF NOT EXISTS timings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    printer TEXT NOT NULL,
    kind TEXT NOT NULL,      -- cooldown, cycle, loop
    seconds REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    printer TEXT NOT NULL,
    key TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    since REAL NOT NULL,
    PRIMARY KEY (printer, key)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_timings ON timings(printer, kind, ts);
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

    # ---- measured durations -------------------------------------------------------
    def add_timing(self, printer: str, kind: str, seconds: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO timings (ts, printer, kind, seconds) VALUES (?,?,?,?)",
                (time.time(), printer, kind, seconds),
            )

    def timings(self, printer: str, last: int = 10) -> dict:
        """Average and latest of the last ``last`` measurements of each kind, all from the real printer."""
        out: dict[str, dict] = {}
        with self._lock:
            kinds = [
                r[0]
                for r in self._db.execute("SELECT DISTINCT kind FROM timings WHERE printer=?", (printer,))
            ]
            for k in kinds:
                rows = [
                    r[0]
                    for r in self._db.execute(
                        "SELECT seconds FROM timings WHERE printer=? AND kind=? ORDER BY id DESC LIMIT ?",
                        (printer, k, last),
                    )
                ]
                out[k] = {"avg": sum(rows) / len(rows), "last": rows[0], "n": len(rows)}
        return out

    # ---- counters (plate bends) ------------------------------------------------------
    def bump(self, printer: str, key: str) -> int:
        with self._lock:
            self._db.execute(
                "INSERT INTO counters (printer, key, value, since) VALUES (?,?,1,?) "
                "ON CONFLICT(printer, key) DO UPDATE SET value = value + 1",
                (printer, key, time.time()),
            )
            return self._db.execute(
                "SELECT value FROM counters WHERE printer=? AND key=?", (printer, key)
            ).fetchone()[0]

    def counter(self, printer: str, key: str) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT value, since FROM counters WHERE printer=? AND key=?", (printer, key)
            ).fetchone()
        return {"value": row[0], "since": row[1]} if row else {"value": 0, "since": None}

    def reset_counter(self, printer: str, key: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO counters (printer, key, value, since) VALUES (?,?,0,?) "
                "ON CONFLICT(printer, key) DO UPDATE SET value = 0, since = excluded.since",
                (printer, key, time.time()),
            )
