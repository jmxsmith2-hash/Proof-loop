"""Connects the monitor to the filament inventory and the production log.

ProofLoop remembers every loop file it makes (filament slots, the slicer's
grams, parts per plate, and filament used per layer). When a monitored printer
runs one of those files, finished loops are charged to the spools that fed
them and counted as production. Files ProofLoop didn't make are never charged:
their usage isn't known.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from proofloop.loopfiles.gcode.usage import layer_usage, used_by_layer
from proofloop.monitor.filament import available, resolve
from proofloop.monitor.runout import by_tray, forecast, run_of
from proofloop.monitor.send import BUSY, feeds
from proofloop.monitor.watch import job_names

log = logging.getLogger(__name__)
SUFFIXES = (".gcode.3mf", ".3mf", ".gcode")


def job_key(name: str) -> str:
    """A loop file's name as the printer reports it once sent: no folder or extension, and only letters,
    digits and ``. _ -`` (send.printer_filename), so "Plate 1.gcode.3mf" and the printer's "Plate_1" match."""
    n = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    for suf in SUFFIXES:
        if n.lower().endswith(suf):
            n = n[: -len(suf)]
            break
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9._-]", "_", n)).strip("._") or n


def plate_record(sf, parts: int) -> dict:
    info = sf.info
    slots = [s for s, _, _ in info.filament_slots]
    p = info.presets
    usage = layer_usage(
        sf.gcode,
        slots,
        {s: p.get(s, {}).get("diameter") for s in slots},
        {s: p.get(s, {}).get("density") for s in slots},
    )
    return {
        "name": sf.name,
        "slots": [list(x) for x in info.filament_slots],
        "grams": {k: round(v, 3) for k, v in info.slot_grams.items()},
        "parts": parts,
        "print_seconds": info.print_seconds,
        "layers": usage,
    }


def register_job(store, filename: str, items: list[tuple], profile_id: str) -> dict:
    """``items``: (SlicedFile, loops, parts per plate) in print order."""
    plates, order = [], []
    for i, (sf, n, parts) in enumerate(items):
        plates.append(plate_record(sf, parts))
        order += [i] * n
    data = {"file": filename, "profile": profile_id, "loops": len(order), "plates": plates, "order": order}
    store.put_job(job_key(filename), data)
    return data


class FilamentTracker:
    """The ``filament`` hooks a PrinterWatch calls."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._sig: dict[str, tuple] = {}
        self._unknown_told: set[str] = set()

    @property
    def inv(self):
        return self.app.inventory

    def job_for(self, s) -> tuple[str | None, dict | None]:
        for n in job_names(s):
            d = self.app.store.job(job_key(n))
            if d:
                return job_key(n), d
        return None, None

    @staticmethod
    def plate_for(data: dict, loop_no: int) -> dict:
        order = data["order"] or [0]
        return data["plates"][order[max(0, min(loop_no, len(order)) - 1)]]

    def _event(self, w, level: str, kind: str, msg: str) -> None:
        w.event(level, kind, msg)

    # ---- hooks --------------------------------------------------------------------
    def loop_layers(self, w, s) -> int | None:
        """Layers in the plate the current loop prints, for files ProofLoop made (batches mix plates)."""
        _key, data = self.job_for(s)
        if not data:
            return None
        usage = self.plate_for(data, w.loop_no).get("layers")
        return usage["layers"] if usage else None

    def loop_done(self, w, s) -> None:
        key, data = self.job_for(s)
        if not data:
            name = (job_names(s) or ["?"])[0]
            if name not in self._unknown_told:
                self._unknown_told.add(name)
                self._event(
                    w,
                    "info",
                    "filament",
                    f"{name} wasn't made by this ProofLoop, so its filament isn't counted",
                )
            self.app.store.add_production(w.name, job_key(name), w.loop_no, 0, 0.0)
            return
        plate = self.plate_for(data, w.loop_no)
        grams = plate["grams"]
        self.app.store.add_production(w.name, key, w.loop_no, plate["parts"], sum(grams.values()))
        if self.app.settings.filament.track_usage:
            self._charge(w, s, key, plate, grams, "loop", "")

    def _counted(self, w, key: str) -> list[int]:
        """Loops of this run counted so far: since the file was made, or since this run began if later."""
        since = max(self.app.store.job_sent(key) or 0.0, getattr(w, "run_since", 0.0))
        return self.app.store.loops_counted(w.name, key, since)

    def loops_missing(self, w, s) -> list[int]:
        """Loops of this run not counted yet (for a run the printer says has finished): their numbers."""
        key, data = self.job_for(s)
        if not data or not key:
            return []
        counted = self._counted(w, key)
        missing = max(0, data["loops"] - len(counted))
        return [n for n in range(1, data["loops"] + 1) if n not in counted][:missing]

    def loops_done(self, w, s) -> int | None:
        """How many loops of this run are counted, for a file ProofLoop made."""
        key, data = self.job_for(s)
        if not data or not key:
            return None
        return min(len(self._counted(w, key)), data["loops"])

    def loop_failed(self, w, s, layers_done: int) -> None:
        key, data = self.job_for(s)
        if not data:
            return
        plate = self.plate_for(data, w.loop_no)
        self.app.store.add_production(w.name, key, w.loop_no, 0, 0.0, ok=False)
        used = used_by_layer(plate.get("layers"), layers_done)
        if used is None:
            self._event(
                w,
                "warning",
                "filament",
                "a loop stopped part-way; its filament use can't be worked out, weigh the spool",
            )
            return
        if self.app.settings.filament.track_usage and sum(used.values()) > 0:
            total = plate["layers"]["layers"]
            self._charge(w, s, key, plate, used, "partial", f"stopped after layer {layers_done} of {total}")

    def _charge(self, w, s, key, plate, grams: dict, kind: str, note: str) -> None:
        trays = s.ams.trays()
        seen = w.trays_this_loop
        hint = seen[0] if len(seen) == 1 else None  # two trays: the AMS switched spools mid-loop
        picks = resolve(self.inv, w.name, [tuple(x) for x in plate["slots"]], trays, hint)
        loose = []
        for sid, material, color in plate["slots"]:
            g = grams.get(sid, 0.0)
            if g <= 0:
                continue
            p = picks[sid]
            self.inv.use(p["spool"], g, kind, w.name, key, w.loop_no, sid, material, color, note or p["how"])
            if not p["spool"]:
                loose.append(f"{g:.1f} g of {material} {color}".strip())
        if loose:
            self._event(
                w,
                "warning",
                "filament_unmatched",
                f"loop {w.loop_no} used {', '.join(loose)} that isn't matched to a spool: assign it in Filament",
            )

    def blocker(self, w, s) -> str | None:
        if not self.app.settings.filament.hold_when_short:
            return None
        _key, data = self.job_for(s)
        if not data or w.loop_no >= len(data["order"]):
            return None
        plate = self.plate_for(data, w.loop_no + 1)
        spools = self.inv.all(archived=False)
        seen = w.trays_this_loop
        hint = seen[-1] if seen else None  # the AMS feeds the next loop from the tray it ended on
        picks = resolve(self.inv, w.name, [tuple(x) for x in plate["slots"]], s.ams.trays(), hint, spools)
        by_id = {sp.id: sp for sp in spools}
        for sid, material, _c in plate["slots"]:
            need = plate["grams"].get(sid, 0.0)
            have, ids = available(self.inv, w.name, picks[sid], spools)
            if have is not None and need > 0 and have < need:
                names = " + ".join(by_id[i].label for i in ids if i in by_id)
                return f"{names} has {have:.0f} g left and the next loop needs {need:.1f} g of {material}"
        return None

    def _rest_of_run(self, w, s, trays: list[dict]) -> tuple[str | None, list[dict[int, float]]]:
        """What the printer still prints of a file ProofLoop made, from the loop it's on: (job, grams per tray)."""
        if not w.loop_no or s.gcode_state.upper() not in BUSY:
            return None, []
        key, data = self.job_for(s)
        if not data or w.loop_no > len(data["order"]):
            return None, []
        slots, run = run_of(data, w.loop_no)
        return key, by_tray(run, feeds(slots, trays))

    def swap_due(self, w, s) -> dict | None:
        """While a run ProofLoop made prints: the first tray it empties before the run ends, and when to swap."""
        trays = s.ams.trays()
        _key, rest = self._rest_of_run(w, s, trays)
        if not rest:
            return None
        rows = forecast(rest, trays, self.inv.all(archived=False), w.name, first=w.loop_no, printing=True)
        due = sorted((r for r in rows if r["status"] in ("swap", "short")), key=lambda r: r["runs_out_in"] or 0)
        if not due:
            return None
        r = due[0]
        text = (f"Spool swap due after loop {r['swap_after']}: {r['label']} ({r['type']}) runs out during "
                f"loop {r['runs_out_in']}" if r["status"] == "swap" else r["text"].split(". ")[0])
        return {"tray": r["tray"], "label": r["label"], "after_loop": r["swap_after"], "runs_out_in": r["runs_out_in"],
                "text": text, "detail": r["text"]}

    def queue_supply(self, runs: list[dict], plate_of: Callable[[str], tuple[list, dict]]) -> None:
        """Adds ``filament`` to each queued job not sent yet: whether its printer's trays last through it, after
        what the printer prints first (the rest of its current run, then the jobs ahead of it in the queue).

        ``plate_of``: run id -> (the plate's filament slots, grams per slot of one loop).
        """
        spools = self.inv.all(archived=False)
        ahead: dict[str, list[dict[int, float]]] = {}
        printing: dict[str, str | None] = {}
        for run in runs:
            for job in run["jobs"]:
                w = self.app.watches.get(job["printer"])
                if job["status"] not in ("waiting", "stopped", "sent") or w is None or not w.link.state.online:
                    continue
                s, name = w.link.state, job["printer"]
                trays = s.ams.trays()
                if name not in ahead:
                    printing[name], ahead[name] = self._rest_of_run(w, s, trays)
                if job["status"] == "sent" and job.get("file") and job_key(job["file"]) == printing[name]:
                    continue  # it's the run printing now, already counted
                try:
                    slots, grams = plate_of(run["id"])
                except (OSError, KeyError, ValueError):
                    continue
                mine = by_tray([grams] * int(job["loops"]), feeds(slots, trays))
                if job["status"] != "sent":
                    rows = forecast(ahead[name] + mine, trays, spools, name, before=len(ahead[name]))
                    job["filament"] = [{k: r[k] for k in ("label", "type", "status", "text", "swap_after")}
                                       for r in rows]
                ahead[name] += mine

    def on_ams(self, w, s) -> None:
        trays = s.ams.trays()
        sig = tuple((t["tray"], t.get("tag"), t.get("type"), t.get("color"), t.get("empty")) for t in trays)
        if not trays or self._sig.get(w.name) == sig:
            return
        self._sig[w.name] = sig
        spools = self.inv.all(archived=False)
        by_tag = {sp.tag: sp for sp in spools if sp.tag}
        for t in trays:
            linked = [sp for sp in spools if sp.printer == w.name and sp.tray == t["tray"]]
            if t.get("empty"):
                for sp in linked:
                    self._unlink(w, sp, f"{sp.label} was taken out of {t['label']}")
                continue
            owner = by_tag.get(t["tag"]) if t.get("tag") else None
            if owner and (owner.printer, owner.tray) != (w.name, t["tray"]):
                owner.printer, owner.tray = w.name, t["tray"]
                owner.drying_temp = owner.drying_temp or t.get("drying_temp")
                owner.drying_hours = owner.drying_hours or t.get("drying_hours")
                self.inv.save(owner)
                self._event(
                    w, "info", "filament", f"{owner.label} is in {t['label']} (read from its RFID tag)"
                )
            for sp in linked:
                if sp is owner:
                    continue
                if t.get("tag") and sp.tag and sp.tag != t["tag"]:
                    self._unlink(w, sp, f"a different spool is in {t['label']} now")
                elif sp.material != t["type"].upper() or (sp.color and t["color"] and sp.color != t["color"]):
                    self._unlink(w, sp, f"{t['label']} now holds {t['type']} {t['color']}, not {sp.label}")

    def _unlink(self, w, sp, why: str) -> None:
        sp.printer, sp.tray = "", None
        self.inv.save(sp)
        self._event(w, "info", "filament", why)

    def link(self, printer: str, tray: int, spool_id: str | None) -> None:
        """Link a spool to an AMS tray by hand (non-RFID spools); None clears the tray."""
        for sp in self.inv.all():
            if sp.printer == printer and sp.tray == tray and sp.id != spool_id:
                sp.printer, sp.tray = "", None
                self.inv.save(sp)
        if spool_id:
            sp = self.inv.get(spool_id)
            sp.printer, sp.tray = printer, tray
            self.inv.save(sp)
