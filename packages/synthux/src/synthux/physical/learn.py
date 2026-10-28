"""Learn from how real prints ended: ClearLoop's print journal -> synthux's G-code checks.

ClearLoop Monitor records every print on the printers it watches (how it ended, the layer it got
to, the printer's HMS alerts, whether parts were left on the plate) and keeps the files it made.
`import_clearloop` reads that journal (read-only), re-checks the G-code that ran, notes what was
printing at the layer a print stopped, and gives each failure its most likely cause:

* first_layer   - it stopped in the first three layers (adhesion, plate, bed temperature)
* filament_feed - the AMS reported a problem (tangle, runout, feed)
* camera        - the printer's own camera inspection stopped it (spaghetti detection)
* flow          - it stopped where the moves ask for close to the most the hot end melts
* cooling       - it stopped on layers that print in a few seconds
* unknown       - none of these; the record is still counted

`history_for` sums this up per printer profile and material, and the G-code check uses it: flow
that has failed on your printer before is flagged, flow your printer has printed fine is no longer
called a problem, first-layer trouble is pointed out, and a file that failed before says where.
These are likely causes from what was printing, not a diagnosis.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from synthux.core.db import Database
from synthux.core.logging import get_logger

log = get_logger(__name__)

CLEARLOOP_DIR = Path.home() / ".clearloop"
# Bambu printer_model_id -> synthux printer profile, for prints whose file can't be found.
MODEL_IDS = {"N7": "bambu_p2s"}
# First block of a Bambu HMS / print_error code: which part of the printer raised it.
HMS_MODULES = {"0700": "AMS", "1200": "AMS lite", "0C00": "camera inspection", "0300": "motion and heaters",
               "0500": "mainboard"}
SUFFIXES = (".gcode.3mf", ".3mf", ".gcode")


def job_key(name: str) -> str:
    n = Path(str(name)).name
    for suf in SUFFIXES:
        if n.lower().endswith(suf):
            return n[: -len(suf)]
    return n


def clearloop_records(data_dir: Path | str = CLEARLOOP_DIR) -> list[dict[str, Any]]:
    path = Path(data_dir) / "clearloop.db"
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        import json

        rows = conn.execute("SELECT id, ts, data FROM outcomes ORDER BY id").fetchall()
    except sqlite3.OperationalError:          # a ClearLoop from before the print journal
        return []
    finally:
        conn.close()
    return [{"id": r[0], "ts": r[1], **json.loads(r[2])} for r in rows]


def find_file(rec: dict[str, Any], folders: list[Path]) -> Path | None:
    if rec.get("file") and Path(rec["file"]).is_file():
        return Path(rec["file"])
    key = job_key(rec.get("job") or "")
    if not key:
        return None
    for folder in folders:
        for suf in SUFFIXES:
            hit = Path(folder) / f"{key}{suf}"
            if hit.is_file():
                return hit
    return None


# ------------------------------------------------------------------------------- evidence

def layer_index(ctx: Any, layer: int, loop_no: int | None) -> int | None:
    """The file's layer index for 'layer N of loop k' as the printer reports it (1-based)."""
    valid = np.flatnonzero(~np.isnan(ctx.layer_h))
    if not len(valid):
        return None
    if loop_no and ctx.metrics.get("loops", 1) > 1:
        loops = sorted(set(ctx.layer_loop[valid].tolist()))
        if 0 < loop_no <= len(loops):
            valid = valid[ctx.layer_loop[valid] == loops[loop_no - 1]]
    return int(valid[min(max(layer, 1), len(valid)) - 1])


def evidence(ctx: Any, layer: int | None, loop_no: int | None) -> dict[str, Any]:
    """What the file does overall, and on the layers up to the one a print stopped at."""
    from synthux.physical.gcode.checks import _at_moves

    t, m = ctx.trace, ctx.metrics
    out: dict[str, Any] = {"peak_flow": m.get("peak_flow_mm3_s"), "typical_flow": m.get("typical_flow_mm3_s"),
                           "nozzle_c": m.get("nozzle_c"), "bed_c": m.get("bed_c"), "fan_pct": m.get("fan_pct"),
                           "first_layer_mm": m.get("first_layer_mm"), "layers": m.get("layers")}
    idx = layer_index(ctx, layer, loop_no) if layer else None
    if idx is None:
        return out
    window = [i for i in range(idx - 2, idx + 1) if i >= 0]
    sel = ctx.extrude & ctx.model & np.isin(t.layer, window)
    long = sel & (ctx.xy >= 1.0)
    times = [float(ctx.time[ctx.model & (t.layer == i)].sum()) for i in window]
    fan = _at_moves(t.fan_events, len(t), default=0.0)
    noz = _at_moves([(i, v) for i, v, _ in t.nozzle_events], len(t))
    feats: dict[str, float] = {}
    for f_id in np.unique(t.feature[sel]) if sel.any() else []:
        feats[t.features[int(f_id)]] = round(float(ctx.time[sel & (t.feature == f_id)].sum()), 1)
    out["at_stop"] = {
        "layer": int(layer), "z_mm": round(float(ctx.layer_z[idx]), 2),
        "peak_flow": round(float(ctx.flow[long].max()), 1) if long.any() else None,
        "layer_seconds": round(min(times), 1) if times else None,
        "fan_pct": round(float(fan[sel].max()) * 100) if sel.any() else None,
        "nozzle_c": round(float(np.nanmax(noz[sel]))) if sel.any() and np.isfinite(noz[sel]).any() else None,
        "features": dict(sorted(feats.items(), key=lambda kv: -kv[1])[:4]),
    }
    return out


def cause_of(rec: dict[str, Any], ev: dict[str, Any], hot_end: float | None, material_flow: float | None) -> str | None:
    if rec.get("outcome") != "failed":
        return None
    codes = [h["code"][:4] for h in rec.get("hms") or []]
    if rec.get("print_error"):
        codes.append(str(rec["print_error"])[:4])
    layer = int(rec.get("layer") or 0)
    if any(c in ("0700", "1200") for c in codes):
        return "filament_feed"
    if layer and layer <= 3:
        return "first_layer"
    if "0C00" in codes:
        return "camera"
    stop = ev.get("at_stop") or {}
    flow = stop.get("peak_flow")
    limit = min(x for x in (hot_end, material_flow) if x) if (hot_end or material_flow) else None
    if flow and limit and flow >= 0.9 * limit:
        return "flow"
    if stop.get("layer_seconds") is not None and stop["layer_seconds"] < 4:
        return "cooling"
    return "unknown"


CAUSE_TEXT = {
    "first_layer": "in the first layers (adhesion: plate, bed temperature, first-layer speed)",
    "filament_feed": "with an AMS alert (filament tangled, ran out or didn't feed)",
    "camera": "when the printer's camera inspection stopped it (spaghetti detection)",
    "flow": "on layers asking for close to the most the hot end melts",
    "cooling": "on layers that print in a few seconds (too little cooling time)",
    "unknown": "with no clear cause in the file",
}


# ------------------------------------------------------------------------------- import

def import_clearloop(db: Database, config_dir: Path | str | None, data_dir: Path | str = CLEARLOOP_DIR,
                     folders: list[Path] | None = None) -> dict[str, int]:
    """Add ClearLoop journal records not imported yet. Returns counts."""
    from synthux.physical.cad.load import load_profiles
    from synthux.physical.gcode.pipeline import analyse, profiles_dir

    folders = [Path(f) for f in (folders or [])] + [Path(data_dir) / "files"]
    have = {r["source_id"] for r in db.all("SELECT source_id FROM print_outcomes WHERE source = 'clearloop'")}
    counts = {"new": 0, "with_gcode": 0, "skipped": 0}
    analysed: dict[str, Any] = {}
    for rec in clearloop_records(data_dir):
        sid = str(rec["id"])
        if sid in have or rec.get("outcome") is None:
            continue
        path = find_file(rec, folders)
        ctx = None
        if path is not None:
            key = str(path)
            if key not in analysed:
                try:
                    analysed[key] = analyse(config_dir, path)[0]
                except Exception as exc:  # noqa: BLE001 - a file we can't read still counts as a print
                    log.warning("could not re-check %s: %s", path, exc)
                    analysed[key] = None
            ctx = analysed[key]
        printer = ctx.printer["name"] if ctx else MODEL_IDS.get(rec.get("model_id") or "")
        material = ctx.material["name"] if ctx else None
        if material is None and rec.get("filaments"):
            try:
                material = load_profiles(profiles_dir(config_dir), printer, rec["filaments"][0]["type"])[1]["name"]
            except Exception:  # noqa: BLE001 - an AMS type synthux doesn't know
                material = rec["filaments"][0]["type"]
        ev = evidence(ctx, rec.get("layer"), rec.get("loop_no") if rec.get("loop_file") else None) if ctx else {}
        hot = mflow = None
        if ctx is not None:
            hot = float(ctx.printer.get("max_flow_mm3_s") or 0) or None
            mflow = float(ctx.material.get("flow_mm3_s") or 0) or None
        db.insert("print_outcomes", {
            "source": "clearloop", "source_id": sid, "ended_at": float(rec.get("ended") or rec.get("ts") or 0),
            "printer_name": rec.get("printer") or "?", "printer": printer, "material": material,
            "job": job_key(rec.get("job") or ""), "loop_no": rec.get("loop_no"), "outcome": rec["outcome"],
            "layer": rec.get("layer"), "total_layers": rec.get("total_layers"), "eject": rec.get("eject"),
            "cause": cause_of(rec, ev, hot, mflow), "evidence_json": ev, "record_json": rec})
        counts["new"] += 1
        counts["with_gcode"] += int(ctx is not None)
    return counts


# ------------------------------------------------------------------------------- history

def history_for(db: Database, printer: str, material: str, job: str | None = None, last: int = 50) -> dict[str, Any]:
    rows = db.all("SELECT * FROM print_outcomes WHERE printer = ? AND material = ? ORDER BY ended_at DESC LIMIT ?",
                  [printer, material, last])
    done = [r for r in rows if r["outcome"] in ("finished", "failed")]
    failed = [r for r in done if r["outcome"] == "failed"]
    by_cause: dict[str, int] = {}
    for r in failed:
        by_cause[r["cause"] or "unknown"] = by_cause.get(r["cause"] or "unknown", 0) + 1
    ok_peaks = sorted((r["evidence_json"] or {}).get("peak_flow") or 0 for r in done if r["outcome"] == "finished")
    ok_peaks = [p for p in ok_peaks if p]
    flow_fails = [((r["evidence_json"] or {}).get("at_stop") or {}).get("peak_flow") for r in failed
                  if r["cause"] == "flow"]
    flow_fails = [f for f in flow_fails if f]
    checked = [r for r in rows if r["eject"]]
    hist: dict[str, Any] = {
        "printer": printer, "material": material, "prints": len(done), "finished": len(done) - len(failed),
        "failed": len(failed), "by_cause": by_cause,
        # a flow the printer has finished at least twice is proven for it
        "proven_flow": ok_peaks[-2] if len(ok_peaks) >= 2 else None,
        "proven_prints": sum(1 for p in ok_peaks if len(ok_peaks) >= 2 and p >= ok_peaks[-2]),
        "failed_flow": min(flow_fails) if flow_fails else None, "flow_failures": len(flow_fails),
        "eject_checked": len(checked), "eject_left": sum(1 for r in checked if r["eject"] == "parts_left"),
    }
    if job:
        same = [r for r in rows if r["job"] == job_key(job)]
        hist["job"] = [{"outcome": r["outcome"], "layer": r["layer"], "loop_no": r["loop_no"], "cause": r["cause"],
                        "ended_at": r["ended_at"], "eject": r["eject"],
                        "hms": [h["code"] for h in (r["record_json"] or {}).get("hms") or []]} for r in same]
    return hist
