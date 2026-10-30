"""One backup file with everything you'd miss on a new computer.

A backup is a zip: ``backup.json`` (settings, imported hardware profiles, spools, usage, production
log and measured timings, from ``printers.db``) plus these from ProofLoop's folder:

- ``results.db``: design checks and what Check has learned;
- ``feedback.json``: the findings you marked *Not a problem*;
- ``references/``: the pictures of each printer's empty plate;
- ``queue/``: queued runs, with their plates;
- ``config/``: your YAML options.

Phone pairing is left out (phones pair again), and so are reports, the kept loop files and the
camera pictures of failed prints, which can be large. Older backups (``backup.json`` alone) still
restore.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from proofloop import __version__
from proofloop.core.paths import RESULTS_DB
from proofloop.core.settings import Settings
from proofloop.loopfiles.profiles import HardwareProfile

FOLDERS = ("references", "queue", "config")
FILES = (RESULTS_DB, "feedback.json")
MAX_RESTORE = 2 * 1024**3  # what a backup may unpack to


def make(app) -> bytes:
    user_profiles = [json.loads(p.to_json()) for p in app.profiles.all() if p.source.get("kind") != "builtin"]
    data = {
        "proofloop_backup": 1,
        "version": __version__,
        "created": time.time(),
        "settings": json.loads(app.settings.model_dump_json()),
        "profiles": user_profiles,
        "store": app.store.dump(),
    }
    root = Path(app.data_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("backup.json", json.dumps(data))
        if (root / RESULTS_DB).is_file():
            z.writestr(RESULTS_DB, _copy_db(root / RESULTS_DB))
        if (root / "feedback.json").is_file():
            z.write(root / "feedback.json", "feedback.json")
        for folder in FOLDERS:
            for f in sorted((root / folder).rglob("*")) if (root / folder).is_dir() else []:
                if f.is_file() and f.name != "last-diff.png" and not f.name.endswith(".tmp"):
                    z.write(f, f.relative_to(root).as_posix())
    return buf.getvalue()


def _copy_db(path: Path) -> bytes:
    """A consistent copy of a database that may be in use (SQLite's own backup)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "copy.db"
        src, dst = sqlite3.connect(path), sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        return out.read_bytes()


def _members(z: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """The files a backup may hold, by name; anything else in the zip is refused."""
    out, total = {}, 0
    for info in z.infolist():
        if info.is_dir():
            continue
        name = PurePosixPath(info.filename)
        if name.is_absolute() or ".." in name.parts or "\\" in info.filename:
            raise ValueError(f"unexpected file in the backup: {info.filename}")
        if not (str(name) in ("backup.json", *FILES) or (len(name.parts) > 1 and name.parts[0] in FOLDERS)):
            raise ValueError(f"unexpected file in the backup: {info.filename}")
        total += info.file_size
        if total > MAX_RESTORE:
            raise ValueError("the backup unpacks to more than 2 GB")
        out[str(name)] = info
    if "backup.json" not in out:
        raise ValueError("this isn't a ProofLoop backup file")
    return out


def read(raw: bytes) -> tuple[dict, zipfile.ZipFile | None, dict[str, zipfile.ZipInfo]]:
    """The backup's ``backup.json``, and the zip with its other files (None for an older backup)."""
    z, members = None, {}
    if zipfile.is_zipfile(io.BytesIO(raw)):
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as e:
            raise ValueError("the backup file is damaged") from e
        members = _members(z)
        raw = z.read(members["backup.json"])
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("proofloop_backup") != 1:
        raise ValueError("this isn't a ProofLoop backup file")
    return data, z, members


def restore(app, raw: bytes) -> dict:
    """Replace everything in this ProofLoop with the backup. Raises ValueError for a bad file."""
    try:
        data, z, members = read(raw)
        settings = Settings.model_validate(data.get("settings") or {})
        profiles = [HardwareProfile.from_dict(p) for p in data.get("profiles") or []]
        if RESULTS_DB in members:
            results = z.read(members[RESULTS_DB])
            _check_db(results)
    except (TypeError, KeyError, zipfile.BadZipFile) as e:
        raise ValueError(str(e)) from e
    root = Path(app.data_dir)
    app.store.restore(data.get("store") or {})
    for p in profiles:
        try:
            app.profiles.save(p)
        except ValueError:
            pass
    for name in list(app.watches):
        app.remove_printer(name)
    if z is not None:
        if RESULTS_DB in members:
            _restore_db(results, root / RESULTS_DB)
        if "feedback.json" in members:
            (root / "feedback.json").write_bytes(z.read(members["feedback.json"]))
        for folder in FOLDERS:
            names = [n for n in members if n.startswith(folder + "/")]
            if not names:
                continue
            shutil.rmtree(root / folder, ignore_errors=True)
            for n in names:
                dest = root / n
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(members[n]))
    app.settings = settings
    app.save_settings()
    for p in settings.printers:
        app._start_watch(p)
    return {
        "restored": True,
        "spools": len(app.inventory.all()),
        "printers": len(settings.printers),
        "queued": len(app.queue.runs()),
        "plate_pictures": sum(len(app.verifier(p).references) for p in settings.printers),
    }


def _check_db(data: bytes) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "check.db"
        path.write_bytes(data)
        con = sqlite3.connect(path)
        try:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("the design-check database in the backup is damaged")
        except sqlite3.DatabaseError as e:
            raise ValueError("the design-check database in the backup is damaged") from e
        finally:
            con.close()


def _restore_db(data: bytes, dest: Path) -> None:
    """Copy into the live database, so a window that has it open sees the restored checks."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "restore.db"
        path.write_bytes(data)
        src, dst = sqlite3.connect(path), sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
