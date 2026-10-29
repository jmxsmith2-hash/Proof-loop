"""AMS trays as the printer reports them in ``print.ams`` and ``print.vt_tray``.

Field meanings and merge rules follow Bambuddy's MQTT client (see
docs/sources.md): reports are partial, so units merge by ``id`` and trays by
``id``; ``tray_type == ""`` means the slot was emptied; all-zero RFID values
mean "no tag read" and never overwrite a tag already seen; ``remain`` is a
percentage, with -1 (or anything outside 0-100) meaning unknown;
``tray_color`` is ``RRGGBBAA``; ``humidity_raw`` is relative humidity in %.
Global tray ids: AMS unit n slot s = n*4 + s, AMS HT units are 128-135 with
one tray, the external spool is 254 (255 on dual-nozzle printers),
``tray_now`` 255 means nothing is loaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ZERO_TAG = {"", "0000000000000000", "00000000000000000000000000000000"}
EXTERNAL = (254, 255)
UNLOADED = 255


def global_id(unit: int, slot: int) -> int:
    if 128 <= unit <= 135:  # AMS HT: one tray, the unit id is the tray id
        return unit
    if unit == 16:  # A2L AMS Lite, normalised by Bambuddy to unit 6 -> trays 24-27
        return 24 + slot
    return unit * 4 + slot


def label(tray: int) -> str:
    if tray in EXTERNAL:
        return "Ext" if tray == 254 else "Ext 2"
    if 128 <= tray <= 135:
        return f"HT-{chr(65 + tray - 128)}"
    if 24 <= tray <= 27:
        return f"Lite {tray - 23}"
    return f"{chr(65 + tray // 4)}{tray % 4 + 1}"


def valid_tray(tray: int) -> bool:
    return 0 <= tray <= 15 or 24 <= tray <= 27 or 128 <= tray <= 135 or tray == 254


@dataclass
class AmsState:
    units: dict[int, dict] = field(default_factory=dict)  # unit id -> unit fields + "trays": {slot: tray}
    external: dict[int, dict] = field(default_factory=dict)  # 254/255 -> tray fields
    tray_now: int = UNLOADED

    def merge_print(self, p: dict) -> None:
        ams = p.get("ams")
        if isinstance(ams, dict):
            if "tray_now" in ams:
                try:
                    self.tray_now = int(ams["tray_now"])
                except (TypeError, ValueError):
                    self.tray_now = UNLOADED
            if isinstance(ams.get("ams"), list):
                for u in ams["ams"]:
                    self._merge_unit(u)
        elif isinstance(ams, list):
            for u in ams:
                self._merge_unit(u)
        vt = p.get("vt_tray")
        if isinstance(vt, dict):
            self._merge_tray(self.external, _int(vt.get("id"), 254), vt)

    def _merge_unit(self, u) -> None:
        if not isinstance(u, dict):
            return
        uid = _int(u.get("id"), -1)
        if uid < 0:
            return
        unit = self.units.setdefault(uid, {"trays": {}})
        for k in ("humidity_raw", "humidity", "temp", "dry_time"):
            if k in u:
                unit[k] = u[k]
        for t in u.get("tray") or []:
            if isinstance(t, dict):
                self._merge_tray(unit["trays"], _int(t.get("id"), -1), t)

    @staticmethod
    def _merge_tray(trays: dict, tid: int, t: dict) -> None:
        if tid < 0:
            return
        old = trays.get(tid, {})
        if t.get("tray_type") == "":  # slot emptied
            trays[tid] = {"empty": True}
            return
        new = {**old, **t}
        new.pop("empty", None)
        for k in ("tag_uid", "tray_uuid"):  # zero tags in periodic updates mean "not read", not "changed"
            if str(t.get(k, "")) in ZERO_TAG and old.get(k):
                new[k] = old[k]
        trays[tid] = new

    def trays(self) -> list[dict]:
        out = []
        for uid in sorted(self.units):
            unit = self.units[uid]
            for slot in sorted(unit["trays"]):
                out.append(_tray(global_id(uid, slot), unit["trays"][slot], unit))
        for tid in sorted(self.external):
            out.append(_tray(tid, self.external[tid], None))
        return out

    def units_dict(self) -> list[dict]:
        return [
            {
                "id": uid,
                "name": f"HT-{chr(65 + uid - 128)}" if uid >= 128 else f"AMS {chr(65 + uid)}",
                "humidity_pct": _float(u.get("humidity_raw")),
                "temp": _float(u.get("temp")),
                "drying_min_left": _int(u.get("dry_time"), 0) or None,
            }
            for uid, u in sorted(self.units.items())
        ]


def _tray(tid: int, t: dict, unit: dict | None) -> dict:
    if t.get("empty") or not t.get("tray_type"):
        return {"tray": tid, "label": label(tid), "empty": True}
    color = str(t.get("tray_color") or "").strip().lstrip("#").upper()
    remain = _int(t.get("remain"), -1)
    uuid, uid = str(t.get("tray_uuid") or ""), str(t.get("tag_uid") or "")
    return {
        "tray": tid,
        "label": label(tid),
        "empty": False,
        "type": str(t.get("tray_type") or ""),
        "sub_brand": str(t.get("tray_sub_brands") or ""),
        "filament_id": str(t.get("tray_info_idx") or ""),
        "color": f"#{color[:6]}" if len(color) >= 6 else "",
        "remain_pct": remain if 0 <= remain <= 100 else None,
        "tag": uuid if uuid not in ZERO_TAG else (uid if uid not in ZERO_TAG else ""),
        "nozzle_temp_min": _float(t.get("nozzle_temp_min")),
        "nozzle_temp_max": _float(t.get("nozzle_temp_max")),
        "drying_temp": _float(t.get("drying_temp")),
        "drying_hours": _float(t.get("drying_time")),
        "humidity_pct": _float(unit.get("humidity_raw")) if unit else None,
    }


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f else None
