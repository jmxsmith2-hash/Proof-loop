"""LARP mode (*More › LARP mode*): a second ProofLoop with pretend printers, to try things on.

Your real ProofLoop keeps watching your printers while the window shows LARP. LARP runs in a
process of its own on its own port, keeps its own folder (``larp/`` inside ProofLoop's) and starts
blank: nothing of your settings, queue, orders or history is copied in, and nothing done in LARP
reaches them. Every printer in it is pretend: a twin of each of yours (same model, nozzle and AMS
filament, refreshed each time LARP starts) and any you add. A printer added any other way in LARP
is pretend too, whatever address it was given, so nothing in LARP can reach a real printer. The
pretend printers print at 1 to 100 times the speed of real ones.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from proofloop.core.paths import home
from proofloop.core.printers import MODEL_IDS
from proofloop.core.schema import write_json

log = logging.getLogger(__name__)
PORT = 8620
ENV = "PROOFLOOP_LARP"  # set in the LARP process: the real window's address, to go back to
SPEED, SLOWEST, FASTEST = 40, 1, 100
# A pretend printer at real speed: seconds per layer, per FarmBoard step, and before a pretend person
# clears a plate the plate check didn't pass.
LAYER_S, STEP_S, PERSON_S = 4.8, 120.0, 1200.0
ACCESS_CODE = "12345678"
NOZZLES = (0.2, 0.4, 0.6, 0.8)
FLOWS = ("Standard", "High Flow")
MATERIALS = ("PLA", "PETG", "ABS", "ASA", "TPU", "PLA-CF", "PETG-CF", "PA-CF")
FILAMENT_IDS = {"PLA": "GFA00", "PETG": "GFG99", "ABS": "GFB99", "ASA": "GFB98", "TPU": "GFU99"}


def inside() -> bool:
    """Is this the LARP process?"""
    return bool(os.environ.get(ENV))


def folder() -> Path:
    """LARP's own folder: ``larp/`` in ProofLoop's (inside LARP, ProofLoop's folder is this one)."""
    return home() if inside() else home() / "larp"


# ---- what LARP's printers are: larp.json in its folder ----------------------------------------
def load() -> dict[str, Any]:
    try:
        spec = json.loads((folder() / "larp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        spec = {}
    spec.setdefault("speed", SPEED)
    spec.setdefault("printers", [])
    spec.setdefault("gone_twins", [])  # twins you removed: not made again
    return spec


def save(spec: dict[str, Any]) -> None:
    write_json(folder() / "larp.json", spec)


def _serial(spec: dict[str, Any]) -> str:
    taken = {p["serial"] for p in spec["printers"]}
    return next(s for n in range(1, 10_000) if (s := f"01P9LARP{n:07d}") not in taken)


def printer(spec: dict[str, Any], name: str, model_id: str = "N7", nozzle: float = 0.4, flow: str = "Standard",
            trays: list[dict] | None = None, twin_of: str | None = None) -> dict[str, Any]:
    """A pretend printer, checked; ValueError says what's wrong."""
    name = " ".join(str(name).split())
    if not name or "/" in name:
        raise ValueError("give the printer a short name without '/'")
    if any(p["name"].lower() == name.lower() for p in spec["printers"]):
        raise ValueError(f"there is already a printer called {name!r}")
    if model_id not in MODEL_IDS:
        raise ValueError(f"unknown printer model {model_id!r}")
    if float(nozzle) not in NOZZLES:
        raise ValueError("the nozzle is 0.2, 0.4, 0.6 or 0.8 mm")
    if flow not in FLOWS:
        raise ValueError("the nozzle flow is Standard or High Flow")
    return {"name": name, "serial": _serial(spec), "model_id": model_id, "nozzle": float(nozzle), "flow": flow,
            "trays": [_tray(t) for t in (trays or [])][:16], "twin_of": twin_of}


def _tray(t: dict, any_type: bool = False) -> dict[str, Any]:
    """A tray: one of MATERIALS for a printer you add; for a twin, whatever the real printer reported."""
    kind = " ".join(str(t.get("type") or "").split()).upper()
    known = kind in MATERIALS or (any_type and 0 < len(kind) <= 24 and all(c.isalnum() or c in "-+ " for c in kind))
    if not known:
        raise ValueError(f"unknown filament {t.get('type')!r}")
    color = str(t.get("color") or "#F2F2F2").lstrip("#").upper()[:6]
    if len(color) != 6 or any(c not in "0123456789ABCDEF" for c in color):
        raise ValueError(f"{t.get('color')!r} isn't a colour like #F2F2F2")
    return {"tray": int(t.get("tray", 0)), "type": kind, "color": "#" + color,
            "sub_brand": str(t.get("sub_brand") or ""), "filament_id": str(t.get("filament_id") or ""),
            "remain_pct": t.get("remain_pct") if isinstance(t.get("remain_pct"), int) else 80}


def twin(cfg, state) -> dict[str, Any]:
    """What a pretend twin of a real printer copies: its model, nozzle and the filament in its AMS."""
    trays = [{"tray": t["tray"], "type": t["type"], "color": t.get("color") or "#F2F2F2",
              "sub_brand": t.get("sub_brand", ""), "filament_id": t.get("filament_id", ""),
              "remain_pct": t.get("remain_pct")}
             for t in state.ams.trays() if not t.get("empty") and t["tray"] < 16]
    return {"model_id": cfg.model_id, "nozzle": state.nozzle_diameter if state.nozzle_diameter in NOZZLES else 0.4,
            "flow": state.nozzle_flow or "Standard", "trays": [_tray(t, any_type=True) for t in trays]}


def sync_twins(spec: dict[str, Any], app) -> dict[str, Any]:
    """A twin for each of your printers, made like it as it is now; twins you removed stay removed."""
    for cfg in app.settings.printers:
        w = app.watches.get(cfg.name)
        if w is None:
            continue
        copy = twin(cfg, w.link.state)
        mine = next((p for p in spec["printers"] if p.get("twin_of") == cfg.name), None)
        if mine:
            mine.update(copy)
        elif cfg.name not in spec["gone_twins"]:
            name = cfg.name
            while any(p["name"].lower() == name.lower() for p in spec["printers"]):
                name += " twin"
            spec["printers"].append({**printer(spec, name, twin_of=cfg.name), **copy})
    return spec


def _ams(trays: list[dict]) -> list[dict]:
    """The trays as a printer reports them."""
    units: dict[int, list] = {}
    for t in trays:
        units.setdefault(t["tray"] // 4, []).append(
            {"id": str(t["tray"] % 4), "tray_type": t["type"], "tray_color": t["color"].lstrip("#") + "FF",
             "remain": t["remain_pct"], "tray_sub_brands": t["sub_brand"], "tray_info_idx":
             t["filament_id"] or FILAMENT_IDS.get(t["type"], "")})
    return [{"id": str(u), "humidity": "3", "temp": "26.5", "tray": units[u]} for u in sorted(units)]


# ---- inside LARP: the pretend printers -----------------------------------------------------
class Farm:
    def __init__(self, spec: dict[str, Any]):
        self.spec = spec
        self.fakes: dict = {}  # serial -> FakePrinter

    def make(self, p: dict[str, Any]):
        from proofloop.monitor.printer.pretend import FakePrinter

        fake = FakePrinter(serial=p["serial"], access_code=p.get("access_code") or ACCESS_CODE, simulate=True,
                           parts_left=0.15)
        fake.state["ams"] = {"ams": _ams(p["trays"]), "tray_now": "255"}
        fake.state["nozzle_diameter"] = str(p["nozzle"])
        fake.state["nozzle_type"] = "HH01" if p["flow"] == "High Flow" else "HS01"
        self.fakes[p["serial"]] = fake
        self.pace(fake)
        return fake

    def pace(self, fake) -> None:
        s = self.spec["speed"]
        fake.layer_s, fake.step_s, fake.person_s = LAYER_S / s, STEP_S / s, PERSON_S / s

    def set_speed(self, speed: int) -> None:
        self.spec["speed"] = speed
        for fake in self.fakes.values():
            self.pace(fake)
        save(self.spec)

    def link(self, cfg):
        """How LARP's monitor talks to a printer: always a pretend one, never the address it was given."""
        from proofloop.monitor.printer.bambu import BambuPrinter

        fake = self.fakes.get(cfg.serial)
        if fake is None:  # added under Printers like a real one: it gets a pretend printer of its own
            p = next((x for x in self.spec["printers"] if x["serial"] == cfg.serial), None)
            if p is None:
                p = {"name": cfg.name, "serial": cfg.serial, "access_code": cfg.access_code, "model_id": cfg.model_id,
                     "nozzle": 0.4, "flow": "Standard", "trays": [_tray({"type": "PLA"})], "twin_of": None}
                self.spec["printers"].append(p)
                save(self.spec)
            fake = self.make(p)
        b = BambuPrinter("127.0.0.1", cfg.serial, cfg.access_code, cfg.model_id, fake.snapshot_url)
        b.mqtt_port, b.ftp_port = fake.mqtt_port, fake.ftp_port
        return b

    def close(self) -> None:
        for fake in self.fakes.values():
            fake.close()


FARM: Farm | None = None  # inside LARP


def _config(p: dict[str, Any]):
    from proofloop.core.settings import PrinterConfig, VerifyConfig

    return PrinterConfig(name=p["name"], host="127.0.0.1", serial=p["serial"],
                         access_code=p.get("access_code") or ACCESS_CODE, model_id=p["model_id"],
                         verify=VerifyConfig(settle_s=0.5))


def prepare(spec: dict[str, Any]) -> None:
    """LARP's settings: blank the first time, but ready to send; its printers are the ones in larp.json."""
    from proofloop.core.settings import SettingsStore
    from proofloop.monitor.printer.pretend import plate_picture
    from proofloop.monitor.vision import BedVerifier

    root = folder()
    store = SettingsStore(root)
    new = not (root / "settings.json").exists()
    s = store.load()
    if new:
        s.send_to_printer = True  # everything you try ends up on a pretend printer
        s.defaults.wait_for_monitor = True
        s.defaults.floor_calibrated = True
    have = {c.serial: c for c in s.printers}
    for p in list(spec["printers"]):
        if p.get("seen") and p["serial"] not in have:  # removed under Printers since LARP last ran
            spec["printers"].remove(p)
            if p.get("twin_of"):
                spec["gone_twins"].append(p["twin_of"])
    s.printers = [have.get(p["serial"]) or _config(p) for p in spec["printers"]] + \
        [c for c in s.printers if c.serial not in {p["serial"] for p in spec["printers"]}]
    for p in spec["printers"]:
        p["seen"] = True
    store.save(s)
    save(spec)
    for c in s.printers:
        refs = BedVerifier(root / "references" / c.name, None)
        if not refs.references:
            refs.add_reference(plate_picture())


def enter(back: str) -> None:
    """Make this process LARP: its folder becomes ProofLoop's. Before anything reads ProofLoop's folder."""
    larp = home() / "larp"
    os.environ["PROOFLOOP_HOME"] = str(larp)
    os.environ[ENV] = back


def run(port: int = PORT) -> int:
    """The LARP process (``proofloop larp``, started by the real ProofLoop): until it's turned off."""
    global FARM
    from proofloop.app.service import Service

    spec = load()
    FARM = Farm(spec)
    try:
        prepare(spec)
        for p in spec["printers"]:
            FARM.make(p)
        svc = Service(port, link_factory=FARM.link)
        svc.start()
        log.info("LARP mode at http://127.0.0.1:%d/ with %d pretend printers", port, len(spec["printers"]))
        svc.wait()
    finally:
        FARM.close()
    return 0


# ---- from the real ProofLoop: turning it on and off ------------------------------------------
def _ask(path: str, body: dict | None = None, timeout: float = 2.0) -> dict | None:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers={"X-ProofLoop": "1"})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except (OSError, ValueError, urllib.error.HTTPError):
        return None


def running() -> bool:
    r = _ask("/proofloop/ping", timeout=1.0)
    return bool(r and r.get("larp"))


def start(app, back: str, timeout: float = 60.0) -> str:
    """Start LARP (if it isn't running) with twins of your printers as they are now; its address."""
    from proofloop.app.autostart import background_python, repo_root

    url = f"http://127.0.0.1:{PORT}/"
    if running():
        return url
    if _ask("/proofloop/ping", timeout=1.0) is not None:
        raise RuntimeError(f"port {PORT} is used by another ProofLoop (proofloop try?); stop it first")
    save(sync_twins(load(), app))
    cmd = [str(background_python()), "-m", "proofloop", "larp", "--port", str(PORT), "--back", back]
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "cwd": str(repo_root() or Path.cwd()), "close_fds": True}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(cmd, **kw)
    end = time.time() + timeout
    while not running():
        if time.time() > end:
            raise RuntimeError(f"LARP didn't start; see {folder() / 'proofloop.log'}")
        time.sleep(0.3)
    return url


def stop() -> bool:
    """Turn LARP off (its pretend prints end; its folder stays for next time). True if it was running."""
    return running() and _ask("/proofloop/quit", {"reason": "LARP mode turned off"}) is not None


# ---- the window's routes, in both ---------------------------------------------------------
def register(api: FastAPI, shell, window_only) -> None:
    async def body_of(request: Request) -> dict:
        window_only(request)
        try:
            body = await request.json()
        except ValueError as e:
            raise HTTPException(400, "send JSON") from e
        if not isinstance(body, dict):
            raise HTTPException(400, "send a JSON object")
        return body

    if not inside():
        @api.get("/proofloop/larp")
        def larp_status(request: Request):
            window_only(request)
            return {"inside": False, "running": running(), "url": f"http://127.0.0.1:{PORT}/"}

        @api.post("/proofloop/larp")
        async def larp_switch(request: Request):
            body = await body_of(request)
            if body.get("on"):
                try:
                    return {"running": True, "url": start(shell.monitor, shell.origin + "/")}
                except (RuntimeError, OSError, ValueError) as e:
                    raise HTTPException(409, str(e)) from e
            stop()
            return {"running": False}

        if shell.service is not None:
            shell.service.on_stop.append(stop)  # quitting ProofLoop ends LARP too
        return

    def state() -> dict[str, Any]:
        spec = FARM.spec if FARM else load()
        return {"inside": True, "back": os.environ.get(ENV), "speed": spec["speed"], "slowest": SLOWEST,
                "fastest": FASTEST, "models": MODEL_IDS, "nozzles": NOZZLES, "flows": FLOWS, "materials": MATERIALS,
                "printers": [{k: p.get(k) for k in ("name", "model_id", "nozzle", "flow", "trays", "twin_of")}
                             for p in spec["printers"]]}

    def farm() -> Farm:
        if FARM is None:
            raise HTTPException(409, "LARP's printers aren't running")
        return FARM

    @api.get("/proofloop/larp")
    def larp_state(request: Request):
        window_only(request)
        return state()

    @api.post("/proofloop/larp/speed")
    async def larp_speed(request: Request):
        body = await body_of(request)
        try:
            speed = int(body.get("speed") or 0)
        except (TypeError, ValueError) as e:
            raise HTTPException(422, "speed is a whole number") from e
        if not SLOWEST <= speed <= FASTEST:
            raise HTTPException(422, f"speed is {SLOWEST} to {FASTEST} times real")
        farm().set_speed(speed)
        return state()

    @api.post("/proofloop/larp/add")
    async def larp_add(request: Request):
        body = await body_of(request)
        f = farm()
        try:
            p = printer(f.spec, body.get("name", ""), str(body.get("model_id") or "N7"), float(body.get("nozzle") or 0.4),
                        str(body.get("flow") or "Standard"), [{**t, "tray": i} for i, t in enumerate(body.get("trays") or [])
                                                             if isinstance(t, dict) and t.get("type")])
        except (TypeError, ValueError) as e:
            raise HTTPException(422, str(e)) from e
        p["seen"] = True
        f.spec["printers"].append(p)
        save(f.spec)
        try:
            shell.monitor.add_printer(_config(p))  # its link is made by Farm.link, which makes the pretend printer
        except ValueError as e:  # a printer of that name added under Printers
            f.spec["printers"].remove(p)
            save(f.spec)
            raise HTTPException(422, str(e)) from e
        from proofloop.monitor.printer.pretend import plate_picture
        from proofloop.monitor.vision import BedVerifier

        BedVerifier(folder() / "references" / p["name"], None).add_reference(plate_picture())
        return state()

    @api.post("/proofloop/larp/remove")
    async def larp_remove(request: Request):
        body = await body_of(request)
        f = farm()
        p = next((x for x in f.spec["printers"] if x["name"] == body.get("name")), None)
        if p is None:
            raise HTTPException(404, "no such pretend printer")
        try:
            shell.monitor.remove_printer(p["name"])
        except KeyError:
            pass
        fake = f.fakes.pop(p["serial"], None)
        if fake:
            fake.close()
        f.spec["printers"].remove(p)
        if p.get("twin_of"):
            f.spec["gone_twins"].append(p["twin_of"])
        save(f.spec)
        return state()
