"""Spread a production run over your printers, and keep it as a queue.

``spread`` works out which printers can print a plate (the model it was sliced for, a hardware
profile for it, the filament loaded) and gives each a share of the loops, so the printers finish at
about the same time: each loop goes to the printer that would finish it first, counting when the
printer is free (its remaining print time) and its own loop time (the plate's print and fixed steps,
plus the cooldown measured on that printer).

A run kept in the ``Queue`` stays on disk (``queue/`` in ProofLoop's folder, with a copy of the
plate). When a printer with a job waiting comes free, ProofLoop tells you once, through the event
log and your notification targets. Nothing in the queue starts by itself: you send or download each
job (sending needs Sending to printers turned on, and your confirmation).

A job you sent or downloaded is followed on its printer from what the printer reports: *printing*
once its file runs there (with the loop it's on), then *done* when the file finishes, or *stopped*
when it fails or is cancelled. Only then is the printer's next job offered.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from proofloop.monitor.journal import CANCELLED_ERROR
from proofloop.monitor.send import BUSY, match_trays
from proofloop.monitor.tracking import job_key
from proofloop.monitor.watch import job_names

MAX_LOOPS = 50  # loops in one file (as Make a loop file allows); longer runs on one printer get several files
FOLLOWED = ("sent", "downloaded", "printing")  # jobs out of the queue and not finished yet
START_WAIT_S = 600  # a sent file the printer hasn't started in this long didn't start


def _free_in(state) -> float:
    """Seconds until the printer is free: 0 when idle, its remaining time while printing."""
    if not state.online:
        return 0.0
    if state.gcode_state.upper() in BUSY:
        return max(60.0, state.remaining_min * 60.0)
    return 0.0


def spread(app, sf, profile_model: str, loop_known_s: float, parts_per_loop: int, parts: int) -> dict[str, Any]:
    """A plan: which printers, how many loops each, when each finishes; and why others are left out."""
    from proofloop.monitor.service import cooldown_for

    if parts_per_loop < 1:
        raise ValueError("this plate has no parts")
    loops = math.ceil(parts / parts_per_loop)
    model = sf.info.printer_model_id or profile_model
    able, left_out = [], []
    for p in app.settings.printers:
        w = app.watches.get(p.name)
        why = []
        if p.model_id != model:
            why.append("another printer model")
        state = w.link.state if w else None
        if state is None or not state.online:
            why.append("not connected")
        elif any(not t.get("empty") for t in state.ams.trays()):
            _, problems = match_trays(sf.info.filament_slots, state.ams.trays())
            why += problems
        elif len(sf.info.filament_slots) > 1:
            why.append("no AMS for a file with several filaments")
        if why:
            left_out.append({"printer": p.name, "why": "; ".join(why)})
            continue
        cool_s, cool_from = cooldown_for(app, p.name, None, 0, 0)
        able.append({"printer": p.name, "free_in": _free_in(state), "cooldown_s": cool_s, "cooldown_from": cool_from})
    if not able:
        return {"loops": loops, "parts": loops * parts_per_loop, "rows": [], "left_out": left_out, "finish_s": None}
    known = [a["cooldown_s"] for a in able if a["cooldown_s"] is not None]
    fallback = sum(known) / len(known) if known else None
    for a in able:
        cool = a["cooldown_s"] if a["cooldown_s"] is not None else fallback
        a["loop_s"] = loop_known_s + (cool or 0.0)
        a["estimated"] = a["cooldown_s"] is None
        a["loops"] = 0
    for _ in range(loops):  # each loop to the printer that would finish it first
        best = min(able, key=lambda a: (a["free_in"] + (a["loops"] + 1) * a["loop_s"], a["printer"]))
        best["loops"] += 1
    rows = []
    for a in sorted(able, key=lambda a: a["printer"]):
        if not a["loops"]:
            continue
        rows.append({"printer": a["printer"], "loops": a["loops"], "parts": a["loops"] * parts_per_loop,
                     "files": math.ceil(a["loops"] / MAX_LOOPS), "starts_in_s": round(a["free_in"]),
                     "finish_s": round(a["free_in"] + a["loops"] * a["loop_s"]),
                     "cooldown": "estimated from your other printers" if a["estimated"] and fallback else
                     ("not measured yet" if a["estimated"] else a["cooldown_from"])})
    return {"loops": loops, "parts": loops * parts_per_loop, "rows": rows, "left_out": left_out,
            "finish_s": max(r["finish_s"] for r in rows) if rows else None,
            "cooldown_known": fallback is not None}


class Queue:
    """Runs you kept, each a copy of the plate, its settings and one job per printer and file."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.path = self.folder / "queue.json"
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, runs: list[dict[str, Any]]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(runs, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._load()

    def add(self, plate_name: str, plate: bytes, settings: dict, plan: dict[str, Any]) -> dict[str, Any]:
        if not plan["rows"]:
            why = "; ".join(f"{x['printer']}: {x['why']}" for x in plan["left_out"]) or "add a printer first"
            raise ValueError(f"no printer can print this plate right now: {why}")
        rid = uuid.uuid4().hex[:10]
        jobs = []
        for row in plan["rows"]:
            left = row["loops"]
            while left > 0:
                n = min(left, MAX_LOOPS)
                jobs.append({"id": uuid.uuid4().hex[:8], "printer": row["printer"], "loops": n, "status": "waiting",
                             "notified": False, "file": None, "at": None})
                left -= n
        run = {"id": rid, "plate": plate_name, "parts": plan["parts"], "created": time.time(),
               "settings": {k: v for k, v in settings.items() if k != "loops"}, "jobs": jobs}
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / f"{rid}.gcode.3mf").write_bytes(plate)
            runs = self._load()
            runs.append(run)
            self._save(runs)
        return run

    def plate(self, rid: str) -> tuple[bytes, str]:
        run = self.get(rid)
        return (self.folder / f"{rid}.gcode.3mf").read_bytes(), run["plate"]

    def get(self, rid: str) -> dict[str, Any]:
        for run in self.runs():
            if run["id"] == rid:
                return run
        raise KeyError(f"no queued run {rid}")

    def job(self, rid: str, jid: str) -> dict[str, Any]:
        for job in self.get(rid)["jobs"]:
            if job["id"] == jid:
                return job
        raise KeyError(f"no job {jid} in that run")

    def mark(self, rid: str, jid: str, **changes) -> None:
        with self._lock:
            runs = self._load()
            for run in runs:
                for job in run["jobs"] if run["id"] == rid else []:
                    if job["id"] == jid:
                        job.update(changes)
            self._save(runs)

    def remove(self, rid: str) -> None:
        with self._lock:
            runs = [r for r in self._load() if r["id"] != rid]
            self._save(runs)
            (self.folder / f"{rid}.gcode.3mf").unlink(missing_ok=True)

    def follow(self, app) -> list[tuple[dict, dict]]:
        """Move jobs on from what their printers report; returns the (run, job) that just ended."""
        out = []
        with self._lock:
            runs = self._load()
            changed = False
            for run in runs:
                for job in run["jobs"]:
                    if job["status"] in FOLLOWED and job.get("file") and self._follow(app, job):
                        changed = True
                        if job["status"] in ("done", "stopped"):
                            out.append((run, job))
            if changed:
                self._save(runs)
        return out

    @staticmethod
    def _follow(app, job: dict) -> bool:
        """One job's next step, from its printer's state. True when the job changed."""
        w = app.watches.get(job["printer"])
        if w is None or not w.link.state.online:
            return False
        s, before = w.link.state, dict(job)
        mine = job_key(job["file"]) in {job_key(n) for n in job_names(s)}
        state = s.gcode_state.upper()
        if job["status"] == "printing":
            if mine and state in BUSY:
                job["loop"] = max(1, w.loop_no)
            elif mine and state == "FAILED":
                job.update(status="stopped", why="cancelled" if s.print_error == CANCELLED_ERROR else
                           f"failed in loop {job.get('loop') or 1}", ended=time.time())
            elif mine and state == "FINISH":
                job.update(status="done", loop=job["loops"], ended=time.time())
            elif not mine:  # the printer moved on to another print before ProofLoop saw this one end
                ended = [o for o in app.store.outcomes(since=job["at"] or 0, printer=job["printer"], limit=200)
                         if job_key(o.get("job") or "") == job_key(job["file"])]
                bad = next((o for o in ended if o["outcome"] in ("failed", "cancelled")), None)
                job.update(status="stopped" if bad else "done", ended=time.time(),
                           why=(bad["outcome"] if bad else None))
        elif mine and state in BUSY:  # sent or downloaded, and now running on its printer
            job.update(status="printing", loop=max(1, w.loop_no))
        elif job["status"] == "sent" and time.time() - (job["at"] or 0) > START_WAIT_S:
            job.update(status="stopped", why="the printer didn't start it", ended=time.time())
        return job != before

    def ready(self, app) -> list[tuple[dict, dict]]:
        """Jobs whose printer just came free and that you haven't been told about yet: (run, job)."""
        out = []
        with self._lock:
            runs = self._load()
            changed = False
            # A printer still on a job you sent has nothing to offer yet (a downloaded one may never run).
            seen = {j["printer"] for run in runs for j in run["jobs"] if j["status"] in ("sent", "printing")}
            for run in runs:
                waiting = [j for j in run["jobs"] if j["status"] == "waiting"]
                for job in waiting:  # only the next job on each printer
                    if job["printer"] in seen:
                        continue
                    seen.add(job["printer"])
                    w = app.watches.get(job["printer"])
                    if job["notified"] or w is None or not w.link.state.online:
                        continue
                    if w.link.state.gcode_state.upper() in BUSY:
                        continue
                    job["notified"] = True
                    changed = True
                    out.append((run, job))
            if changed:
                self._save(runs)
        return out
