"""Print journal: how every print on a watched printer ended, for learning what goes wrong.

For each print (each loop of a loop file) it records the file, how it ended (finished, failed,
cancelled), the layer it got to, the printer's HMS alerts and error code, the highest nozzle and
bed targets, the AMS trays it fed from, and whether the plate check afterwards found the plate
empty. A camera picture is kept when a print fails or parts are left behind. Files ClearLoop made
are kept too (``files/``), so a tool such as synthux can re-check the exact G-code that ran.

Nothing here changes what the monitor does; every hook is wrapped so a journal problem can never
stop a plate check.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .monitor import is_loop_job, job_names
from .printer.ams import valid_tray

log = logging.getLogger(__name__)

# Bambu reports a print stopped from the screen or the app as FAILED with this print_error
# (0300400C), which Bambuddy reads as "cancelled"; anything else is a real failure.
CANCELLED_ERROR = 0x0300400C
EJECT_WINDOW_S = 6 * 3600  # a plate check this soon after a print belongs to it
KEEP_FILES = 100


def job_key(name: str) -> str:
    from .tracking import job_key as key

    return key(name)


class PrintJournal:
    def __init__(self, store, data_dir: str | Path, models: dict[str, str] | None = None):
        self.store = store
        self.dir = Path(data_dir)
        self.models = models if models is not None else {}  # printer name -> model id
        self._open: dict[str, dict] = {}  # printer -> record of the print running now
        self._last: dict[str, tuple[int, float]] = {}  # printer -> (outcome id, when it ended)
        self._closed: dict[str, tuple[str, int]] = {}  # printer -> the print just recorded (job, loop)

    # ---- files ClearLoop makes ------------------------------------------------------
    def keep_file(self, filename: str, data: bytes) -> Path | None:
        """Keep a copy of a file ClearLoop made, so the G-code that ran can be checked later."""
        try:
            folder = self.dir / "files"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{job_key(filename)}.gcode.3mf"
            path.write_bytes(data)
            kept = sorted(folder.glob("*.gcode.3mf"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in kept[KEEP_FILES:]:
                old.unlink(missing_ok=True)
            return path
        except OSError:
            log.warning("could not keep a copy of %s", filename)
            return None

    def file_for(self, job: str) -> Path | None:
        path = self.dir / "files" / f"{job_key(job)}.gcode.3mf"
        return path if path.is_file() else None

    # ---- hooks the monitor calls ------------------------------------------------------
    def on_state(self, w, s) -> None:
        if s.gcode_state not in ("RUNNING", "PREPARE"):
            return
        names = job_names(s)
        if not names:
            return
        key = (names[0], w.loop_no if is_loop_job(s) else 0)
        rec = self._open.get(w.name)
        if rec is None and self._closed.get(w.name) == key:
            return  # still RUNNING after the print ended (a loop file's cooldown and sweep)
        if rec is None or (rec["job"], rec["loop_no"]) != key:
            if rec is not None:
                self._close(w, rec, "unknown", s)  # a new print started before we saw this one end
            rec = self._start(w, s, key)
        if s.layer_num:
            rec["layer"] = max(rec["layer"], s.layer_num)
        rec["total_layers"] = s.total_layers or rec["total_layers"]
        if s.nozzle_target > 5:  # 1..5 °C are FarmBoard signals, not heat
            rec["nozzle_target_max"] = max(rec["nozzle_target_max"], s.nozzle_target)
        rec["bed_target_max"] = max(rec["bed_target_max"], s.bed_target)
        for h in s.hms:
            code = {"code": h.code_str, "level": h.level}
            if code not in rec["hms"]:
                rec["hms"].append(code)
        if s.print_error:
            rec["print_error"] = f"{s.print_error:08X}"
        tray = s.ams.tray_now
        if valid_tray(tray) and tray not in rec["trays"]:
            rec["trays"].append(tray)
            t = next((x for x in s.ams.trays() if x["tray"] == tray), None)
            if t and t.get("type"):
                rec["filaments"].append({"tray": tray, "type": t["type"], "color": t.get("color")})
        rec["firmware"] = s.firmware or rec["firmware"]

    def printed(self, w, s) -> None:
        """The current print (or loop) finished."""
        rec = self._open.get(w.name)
        if rec is not None:
            self._close(w, rec, "finished", s)

    def failed(self, w, s) -> None:
        rec = self._open.get(w.name)
        if rec is None:
            names = job_names(s)
            if not names:
                return
            rec = self._start(w, s, (names[0], w.loop_no if is_loop_job(s) else 0))
            rec["layer"] = s.layer_num
        if s.print_error:                       # the error arrives with the FAILED report itself
            rec["print_error"] = f"{s.print_error:08X}"
        for h in s.hms:
            code = {"code": h.code_str, "level": h.level}
            if code not in rec["hms"]:
                rec["hms"].append(code)
        rec["layer"] = max(rec["layer"], s.layer_num)
        cancelled = s.print_error == CANCELLED_ERROR
        rid = self._close(w, rec, "cancelled" if cancelled else "failed", s)
        if not cancelled and rid is not None:
            w.run_async(lambda: self._picture(w, rid, "failed"))

    def plate_checked(self, w, clear: bool | None, jpeg: bytes | None) -> None:
        last = self._last.get(w.name)
        if not last or time.time() - last[1] > EJECT_WINDOW_S or clear is None:
            return
        rid = last[0]
        data = self.store.outcome(rid)
        if data is None or data.get("eject") == "parts_left":
            return
        data["eject"] = "clear" if clear else "parts_left"
        if not clear and jpeg:
            data["eject_picture"] = self._save_picture(rid, "parts-left", jpeg)
        self.store.update_outcome(rid, data)

    # ---- records -----------------------------------------------------------------------
    def _start(self, w, s, key: tuple[str, int]) -> dict:
        job, loop_no = key
        rec = {
            "printer": w.name,
            "model_id": self.models.get(w.name, ""),
            "job": job,
            "loop_no": loop_no,
            "loop_file": is_loop_job(s),
            "started": time.time(),
            "ended": None,
            "outcome": None,
            "layer": 0,
            "total_layers": s.total_layers,
            "nozzle_target_max": 0.0,
            "bed_target_max": 0.0,
            "hms": [],
            "print_error": None,
            "trays": [],
            "filaments": [],
            "firmware": s.firmware,
            "file": None,
            "eject": None,
        }
        kept = self.file_for(job)
        if kept:
            rec["file"] = str(kept)
        self._open[w.name] = rec
        return rec

    def _close(self, w, rec: dict, outcome: str, s) -> int | None:
        self._open.pop(w.name, None)
        self._closed[w.name] = (rec["job"], rec["loop_no"])
        rec["outcome"] = outcome
        rec["ended"] = time.time()
        if outcome == "finished" and rec["total_layers"]:
            rec["layer"] = max(rec["layer"], rec["total_layers"])
        try:
            rid = self.store.add_outcome(rec)
        except Exception:
            log.exception("%s: could not record the print", w.name)
            return None
        self._last[w.name] = (rid, rec["ended"])
        return rid

    def _picture(self, w, rid: int, what: str) -> None:
        try:
            jpeg = w.link.snapshot()
        except Exception:
            return  # no camera picture: the record is still kept
        data = self.store.outcome(rid)
        if data is not None:
            data["picture"] = self._save_picture(rid, what, jpeg)
            self.store.update_outcome(rid, data)

    def _save_picture(self, rid: int, what: str, jpeg: bytes) -> str | None:
        try:
            folder = self.dir / "outcomes"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{rid}-{what}.jpg"
            path.write_bytes(jpeg)
            return str(path)
        except OSError:
            return None
