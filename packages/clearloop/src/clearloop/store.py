"""SQLite persistence: job queue, loop history, events and plate wear."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    plate INTEGER NOT NULL DEFAULT 1,
    printer TEXT,              -- pin to one printer, or NULL for any compatible printer
    models TEXT NOT NULL DEFAULT '[]',
    materials TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued, printing, done, failed, cancelled
    assigned_to TEXT,
    order_ref TEXT,
    sku TEXT,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    message TEXT
);
CREATE TABLE IF NOT EXISTS loops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer TEXT NOT NULL,
    job_id INTEGER,
    started REAL NOT NULL,
    finished REAL,
    result TEXT,               -- cleared, recovered, blocked
    retries INTEGER NOT NULL DEFAULT 0,
    trigger_seen INTEGER NOT NULL DEFAULT 0,
    eject_s REAL,
    peak_current_a REAL,
    changed_fraction REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    printer TEXT,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS plates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer TEXT NOT NULL,
    type TEXT NOT NULL,
    rated_cycles INTEGER NOT NULL,
    installed REAL NOT NULL,
    retired REAL,
    cycles INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority, id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_loops_printer ON loops(printer, id);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    def _q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _insert(self, sql: str, args=()) -> int:
        with self._lock:
            return int(self._db.execute(sql, args).lastrowid)

    def _x(self, sql: str, args=()) -> int:
        """Run an UPDATE/DELETE and return the number of rows it touched."""
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # ---- jobs --------------------------------------------------------------
    def add_job(
        self,
        name,
        file_path,
        plate=1,
        printer=None,
        models=(),
        materials=(),
        priority=0,
        order_ref=None,
        sku=None,
    ) -> int:
        return self._insert(
            "INSERT INTO jobs (name, file_path, plate, printer, models, materials, priority, order_ref, sku, created)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                str(file_path),
                plate,
                printer,
                json.dumps(list(models)),
                json.dumps(list(materials)),
                priority,
                order_ref,
                sku,
                time.time(),
            ),
        )

    def job(self, job_id: int) -> dict | None:
        rows = self._q("SELECT * FROM jobs WHERE id=?", (job_id,))
        return _job(rows[0]) if rows else None

    def jobs(self, status: str | None = None, limit: int = 200) -> list[dict]:
        if status:
            rows = self._q(
                "SELECT * FROM jobs WHERE status=? ORDER BY priority DESC, id LIMIT ?", (status, limit)
            )
        else:
            rows = self._q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        return [_job(r) for r in rows]

    def next_job(self, printer: str, model: str, loaded: list[str] | None) -> dict | None:
        """Highest-priority queued job this printer can run with what it has loaded."""
        for job in self.jobs("queued", limit=500):
            if job["printer"] and job["printer"] != printer:
                continue
            if job["models"] and model not in job["models"]:
                continue
            if loaded and job["materials"]:
                have = {t.upper() for t in loaded}
                if not all(_base(m) in {_base(h) for h in have} for m in job["materials"]):
                    continue
            return job
        return None

    def claim_job(self, job_id: int, printer: str) -> bool:
        n = self._x(
            "UPDATE jobs SET status='printing', assigned_to=?, started=? WHERE id=? AND status='queued'",
            (printer, time.time(), job_id),
        )
        return n == 1

    def finish_job(self, job_id: int, status: str, message: str = "") -> None:
        self._x(
            "UPDATE jobs SET status=?, finished=?, message=? WHERE id=?",
            (status, time.time(), message, job_id),
        )

    def requeue_job(self, job_id: int, message: str = "") -> None:
        self._x(
            "UPDATE jobs SET status='queued', assigned_to=NULL, started=NULL, message=? WHERE id=?",
            (message, job_id),
        )

    def cancel_job(self, job_id: int) -> bool:
        return (
            self._x(
                "UPDATE jobs SET status='cancelled', finished=? WHERE id=? AND status='queued'",
                (time.time(), job_id),
            )
            == 1
        )

    # ---- loops -------------------------------------------------------------
    def start_loop(self, printer: str, job_id: int | None) -> int:
        return self._insert(
            "INSERT INTO loops (printer, job_id, started) VALUES (?,?,?)", (printer, job_id, time.time())
        )

    def update_loop(self, loop_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE loops SET {cols} WHERE id=?", (*fields.values(), loop_id))

    def loops(self, printer: str | None = None, limit: int = 100) -> list[dict]:
        if printer:
            return self._q("SELECT * FROM loops WHERE printer=? ORDER BY id DESC LIMIT ?", (printer, limit))
        return self._q("SELECT * FROM loops ORDER BY id DESC LIMIT ?", (limit,))

    # ---- events ------------------------------------------------------------
    def add_event(
        self, printer: str | None, level: str, kind: str, message: str, data: dict | None = None
    ) -> dict:
        ts = time.time()
        eid = self._insert(
            "INSERT INTO events (ts, printer, level, kind, message, data) VALUES (?,?,?,?,?,?)",
            (ts, printer, level, kind, message, json.dumps(data) if data else None),
        )
        return {
            "id": eid,
            "ts": ts,
            "printer": printer,
            "level": level,
            "kind": kind,
            "message": message,
            "data": data,
        }

    def events(self, limit: int = 100, printer: str | None = None) -> list[dict]:
        if printer:
            rows = self._q("SELECT * FROM events WHERE printer=? ORDER BY id DESC LIMIT ?", (printer, limit))
        else:
            rows = self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["data"] = json.loads(r["data"]) if r["data"] else None
        return rows

    # ---- plates ------------------------------------------------------------
    def active_plate(self, printer: str, type_: str = "textured_pei", rated_cycles: int = 1500) -> dict:
        rows = self._q(
            "SELECT * FROM plates WHERE printer=? AND retired IS NULL ORDER BY id DESC LIMIT 1", (printer,)
        )
        if rows:
            return rows[0]
        self._insert(
            "INSERT INTO plates (printer, type, rated_cycles, installed) VALUES (?,?,?,?)",
            (printer, type_, rated_cycles, time.time()),
        )
        return self.active_plate(printer, type_, rated_cycles)

    def plate_cycle(self, printer: str, retried: bool) -> dict:
        plate = self.active_plate(printer)
        self._x(
            "UPDATE plates SET cycles=cycles+1, retries=retries+? WHERE id=?",
            (1 if retried else 0, plate["id"]),
        )
        return self.active_plate(printer)

    def replace_plate(self, printer: str, type_: str, rated_cycles: int) -> dict:
        self._x("UPDATE plates SET retired=? WHERE printer=? AND retired IS NULL", (time.time(), printer))
        return self.active_plate(printer, type_, rated_cycles)

    # ---- key/value -----------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._x(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- stats -------------------------------------------------------------
    def stats(self, printer: str | None = None, since: float = 0) -> dict:
        where, args = "WHERE started >= ?", [since]
        if printer:
            where += " AND printer = ?"
            args.append(printer)
        rows = self._q(
            f"SELECT printer, COUNT(*) AS loops,"
            f" SUM(result='cleared') AS cleared, SUM(result='recovered') AS recovered,"
            f" SUM(result='blocked') AS blocked, SUM(trigger_seen=0 AND finished IS NOT NULL) AS missed_triggers,"
            f" AVG(finished - started) AS avg_cycle_s, AVG(eject_s) AS avg_eject_s"
            f" FROM loops {where} GROUP BY printer",
            args,
        )
        for r in rows:
            done = (r["cleared"] or 0) + (r["recovered"] or 0) + (r["blocked"] or 0)
            r["success_rate"] = (
                round(((r["cleared"] or 0) + (r["recovered"] or 0)) / done, 4) if done else None
            )
            r["first_try_rate"] = round((r["cleared"] or 0) / done, 4) if done else None
        return {"printers": rows}


def _base(material: str) -> str:
    return material.upper().split()[0].split("-")[0]


def _job(row: dict) -> dict:
    row["models"] = json.loads(row["models"])
    row["materials"] = json.loads(row["materials"])
    return row
