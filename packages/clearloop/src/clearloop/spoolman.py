"""Optional link to a Spoolman server (https://github.com/Donkie/Spoolman).

Endpoints and field meanings follow Bambuddy's Spoolman client (see
docs/sources.md): ``GET /api/v1/spool``, ``PUT /api/v1/spool/{id}/use`` with
``{"use_weight": grams}``, ``PATCH /api/v1/spool/{id}`` with
``{"remaining_weight": grams}``. Net weight is the spool's ``initial_weight``,
else its filament's ``weight``; the empty-spool weight is the spool's
``spool_weight``, else the filament's, else the vendor's. ClearLoop adds no
weight of its own when Spoolman has none.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class SpoolmanError(RuntimeError):
    pass


class Spoolman:
    def __init__(self, url: str, timeout: float = 10.0):
        self.base = url.rstrip("/") + "/api/v1"
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise SpoolmanError(f"Spoolman answered {e.code} for {method} {path}") from e
        except (urllib.error.URLError, OSError) as e:
            raise SpoolmanError(f"can't reach Spoolman at {self.base}: {e}") from e
        return json.loads(raw) if raw else None

    def spools(self) -> list[dict]:
        out = self._req("GET", "/spool?allow_archived=true")
        if not isinstance(out, list):
            raise SpoolmanError("Spoolman sent something that isn't a spool list")
        return out

    def use(self, spool_id: int, grams: float) -> None:
        self._req("PUT", f"/spool/{spool_id}/use", {"use_weight": round(grams, 2)})

    def set_remaining(self, spool_id: int, grams: float) -> None:
        self._req("PATCH", f"/spool/{spool_id}", {"remaining_weight": round(grams, 2)})

    def use_later(self, spool_id: int, grams: float, emit) -> None:
        self._later(lambda: self.use(spool_id, grams), emit)

    def set_remaining_later(self, spool_id: int, grams: float, emit) -> None:
        self._later(lambda: self.set_remaining(spool_id, grams), emit)

    @staticmethod
    def _later(fn, emit) -> None:
        def run():
            try:
                fn()
            except SpoolmanError as e:
                emit("warning", "spoolman", f"Spoolman not updated: {e}")

        threading.Thread(target=run, daemon=True, name="clearloop-spoolman").start()


def to_spool(sm: dict) -> dict:
    """A Spoolman spool as ClearLoop spool fields (no values added that Spoolman doesn't have)."""
    f = sm.get("filament") or {}
    vendor = f.get("vendor") or {}
    color = str(f.get("color_hex") or "").lstrip("#")
    tag = ""
    extra = sm.get("extra") or {}
    if extra.get("tag"):
        try:
            tag = str(json.loads(extra["tag"]))
        except (TypeError, ValueError):
            tag = str(extra["tag"])

    def pos(*vals):
        for v in vals:
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
        return None

    price = sm.get("price") if isinstance(sm.get("price"), (int, float)) else f.get("price")
    return {
        "name": str(f.get("name") or ""),
        "brand": str(vendor.get("name") or ""),
        "material": str(f.get("material") or "?"),
        "color": color[:6],
        "price": float(price) if isinstance(price, (int, float)) and price >= 0 else None,
        "net_g": pos(sm.get("initial_weight"), f.get("weight")),
        "tare_g": pos(sm.get("spool_weight"), f.get("spool_weight"), vendor.get("empty_spool_weight")),
        "remaining_g": float(sm["remaining_weight"])
        if isinstance(sm.get("remaining_weight"), (int, float))
        else None,
        "diameter": pos(f.get("diameter")),
        "density": pos(f.get("density")),
        "location": str(sm.get("location") or ""),
        "lot": str(sm.get("lot_nr") or ""),
        "notes": str(sm.get("comment") or ""),
        "archived": bool(sm.get("archived")),
        "tag": tag,
        "spoolman_id": int(sm["id"]),
    }
