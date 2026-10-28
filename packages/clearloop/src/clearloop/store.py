"""SQLite store: monitor events, plate checks, timings, filament inventory and usage, made files."""

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
CREATE TABLE IF NOT EXISTS spools (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    spool_id TEXT,           -- NULL: not matched to a spool yet
    grams REAL NOT NULL,     -- positive = used, negative = added back
    kind TEXT NOT NULL,      -- loop, partial, manual, weigh, refill
    printer TEXT,
    job TEXT,
    loop_no INTEGER,
    slot TEXT,
    material TEXT,
    color TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    name TEXT PRIMARY KEY,   -- normalised file name of a loop file ClearLoop made
    ts REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    printer TEXT NOT NULL,   -- '' = any printer
    slot_key TEXT NOT NULL,  -- TYPE|#COLOR
    spool_id TEXT NOT NULL,
    PRIMARY KEY (printer, slot_key)
);
CREATE TABLE IF NOT EXISTS production (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    printer TEXT NOT NULL,
    job TEXT,
    loop_no INTEGER,
    parts INTEGER NOT NULL,
    grams REAL NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,        -- when the print ended
    printer TEXT NOT NULL,
    job TEXT,
    outcome TEXT NOT NULL,   -- finished, failed, cancelled, unknown
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_ts ON outcomes(ts);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_spool ON usage(spool_id, ts);
CREATE INDEX IF NOT EXISTS idx_production_ts ON production(ts);
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

    # ---- filament ------------------------------------------------------------------
    def spools(self) -> list[dict]:
        with self._lock:
            return [json.loads(r[0]) for r in self._db.execute("SELECT data FROM spools ORDER BY rowid")]

    def put_spool(self, spool: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO spools (id, data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (spool["id"], json.dumps(spool)),
            )

    def delete_spool(self, sid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM spools WHERE id=?", (sid,))
            self._db.execute("DELETE FROM assignments WHERE spool_id=?", (sid,))
            self._db.execute("UPDATE usage SET spool_id=NULL WHERE spool_id=?", (sid,))

    def add_usage(self, **row) -> int:
        row.setdefault("ts", time.time())
        cols = ",".join(row)
        with self._lock:
            cur = self._db.execute(
                f"INSERT INTO usage ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values())
            )
        return cur.lastrowid

    def usage(
        self, since: float = 0, limit: int = 500, spool_id: str | None = None, unmatched=False
    ) -> list[dict]:
        q, args = "SELECT * FROM usage WHERE ts>=?", [since]
        if spool_id:
            q += " AND spool_id=?"
            args.append(spool_id)
        if unmatched:
            q += " AND spool_id IS NULL AND kind IN ('loop','partial')"
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._db.execute(q, args)]

    def usage_row(self, uid: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM usage WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None

    def set_usage_spool(self, uid: int, spool_id: str | None) -> None:
        with self._lock:
            self._db.execute("UPDATE usage SET spool_id=? WHERE id=?", (spool_id, uid))

    def delete_usage(self, uid: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM usage WHERE id=?", (uid,))

    def assignments(self) -> dict[tuple[str, str], str]:
        with self._lock:
            return {
                (r[0], r[1]): r[2]
                for r in self._db.execute("SELECT printer, slot_key, spool_id FROM assignments")
            }

    def assign(self, printer: str, slot_key: str, spool_id: str | None) -> None:
        with self._lock:
            if spool_id:
                self._db.execute(
                    "INSERT INTO assignments VALUES (?,?,?) ON CONFLICT(printer, slot_key) "
                    "DO UPDATE SET spool_id=excluded.spool_id",
                    (printer, slot_key, spool_id),
                )
            else:
                self._db.execute(
                    "DELETE FROM assignments WHERE printer=? AND slot_key=?", (printer, slot_key)
                )

    # ---- files ClearLoop made, so the monitor knows what a running job uses -------------
    def put_job(self, name: str, data: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (name, ts, data) VALUES (?,?,?) ON CONFLICT(name) "
                "DO UPDATE SET ts=excluded.ts, data=excluded.data",
                (name, time.time(), json.dumps(data)),
            )

    def job(self, name: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT data FROM jobs WHERE name=?", (name,)).fetchone()
        return json.loads(r[0]) if r else None

    def jobs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT name, ts, data FROM jobs ORDER BY ts DESC LIMIT ?", (limit,))
            return [{"name": r[0], "ts": r[1], **json.loads(r[2])} for r in rows]

    # ---- print journal ------------------------------------------------------------
    def add_outcome(self, data: dict) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO outcomes (ts, printer, job, outcome, data) VALUES (?,?,?,?,?)",
                (data.get("ended") or time.time(), data["printer"], data.get("job"), data["outcome"],
                 json.dumps(data)),
            )
            return int(cur.lastrowid)

    def update_outcome(self, oid: int, data: dict) -> None:
        with self._lock:
            self._db.execute("UPDATE outcomes SET data=?, outcome=? WHERE id=?",
                             (json.dumps(data), data["outcome"], oid))

    def outcome(self, oid: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT data FROM outcomes WHERE id=?", (oid,)).fetchone()
        return json.loads(r[0]) if r else None

    def outcomes(self, since: float = 0, limit: int = 500, printer: str | None = None) -> list[dict]:
        q, args = "SELECT id, ts, data FROM outcomes WHERE ts>=?", [since]
        if printer:
            q, args = q + " AND printer=?", [*args, printer]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"id": r[0], "ts": r[1], **json.loads(r[2])} for r in rows]

    # ---- production ----------------------------------------------------------------
    def add_production(self, printer: str, job: str, loop_no: int, parts: int, grams: float, ok=True) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO production (ts, printer, job, loop_no, parts, grams, ok) VALUES (?,?,?,?,?,?,?)",
                (time.time(), printer, job, loop_no, parts, grams, int(ok)),
            )

    def production(self, since: float = 0) -> list[dict]:
        with self._lock:
            return [
                dict(r)
                for r in self._db.execute("SELECT * FROM production WHERE ts>=? ORDER BY id", (since,))
            ]

    def dump(self) -> dict:
        """Everything worth backing up (events and checks are logs, not state)."""
        with self._lock:
            out = {}
            for t in ("spools", "usage", "jobs", "assignments", "production", "timings", "counters", "outcomes"):
                out[t] = [dict(r) for r in self._db.execute(f"SELECT * FROM {t}")]
        return out

    def restore(self, data: dict) -> None:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for t in ("spools", "usage", "jobs", "assignments", "production", "timings", "counters",
                          "outcomes"):
                    rows = data.get(t)
                    if rows is None:
                        continue
                    cols = [r[1] for r in self._db.execute(f"PRAGMA table_info({t})")]
                    self._db.execute(f"DELETE FROM {t}")
                    for r in rows:
                        keep = {k: r[k] for k in cols if k in r}
                        self._db.execute(
                            f"INSERT INTO {t} ({','.join(keep)}) VALUES ({','.join('?' * len(keep))})",
                            tuple(keep.values()),
                        )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
