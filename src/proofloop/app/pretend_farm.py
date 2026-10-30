"""``proofloop try``: the whole of ProofLoop with three pretend printers, no hardware needed.

The printers are pretend (``proofloop.monitor.printer.pretend``), but everything ProofLoop does
with them is real: it connects over the same encrypted protocol, copies loop files over FTPS,
starts them, follows every layer and FarmBoard step, checks the plate with the camera at each loop
end and resumes the next loop, counts parts and filament, and fills the history and the summary.
The printers print about 40 times faster than real ones; now and then a part stays on the plate
and a pretend person clears it.

It keeps its own folder (``try/`` inside ProofLoop's), so your real printers, settings and history
are never touched, and runs on its own port (8610), alongside your real ProofLoop.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import uuid
from pathlib import Path

from proofloop.core.paths import home

log = logging.getLogger(__name__)
PORT = 8610
NAMES = ("Pretend P2S 1", "Pretend P2S 2", "Pretend P2S 3")
COLORS = (("PLA", "F2F2F2"), ("PLA", "F2F2F2"), ("PLA", "F2F2F2"))


def folder() -> Path:
    """The pretend farm's own folder: ``try/`` in ProofLoop's (while it runs, ProofLoop's folder is this one)."""
    return home() if os.environ.get("PROOFLOOP_PRETEND") else home() / "try"


def _trays(i: int) -> list[dict]:
    material, color = COLORS[i]
    return [{"id": "0", "humidity": "3", "temp": "26.5", "tray": [
        {"id": "0", "tray_type": material, "tray_color": color + "FF", "remain": 82 - 9 * i,
         "tray_uuid": f"{0x50E7E4D0 + i:08X}" + "0" * 24, "tray_sub_brands": "PLA Basic", "tray_info_idx": "GFA00"},
        {"id": "1", "tray_type": "PETG", "tray_color": "1F1F1FFF", "remain": 64},
    ]}]


def start_printers():
    from proofloop.monitor.printer.pretend import FakePrinter

    farm = []
    for i, _ in enumerate(NAMES):
        p = FakePrinter(serial=f"22E8BJ5{i:08d}", simulate=True, layer_s=0.12, step_s=3.0, parts_left=0.15,
                        person_s=30.0)
        p.state["ams"] = {"ams": _trays(i), "tray_now": "255"}
        farm.append(p)
    return farm


def link_factory(farm):
    from proofloop.monitor.printer.bambu import BambuPrinter

    by_serial = {p.serial: p for p in farm}

    def link(cfg):
        fake = by_serial.get(cfg.serial)
        b = BambuPrinter(cfg.host, cfg.serial, cfg.access_code, cfg.model_id,
                         fake.snapshot_url if fake else cfg.snapshot_url)
        if fake:
            b.mqtt_port, b.ftp_port = fake.mqtt_port, fake.ftp_port
        return b

    return link


def prepare(farm, fresh: bool = False) -> bool:
    """Settings for the pretend farm, in its own folder. True when it's new (nothing printed yet)."""
    import shutil

    from proofloop.core.settings import PrinterConfig, SettingsStore, VerifyConfig
    from proofloop.monitor.printer.pretend import plate_picture
    from proofloop.monitor.vision import BedVerifier

    root = folder()
    if fresh:
        shutil.rmtree(root, ignore_errors=True)
    new = not (root / "settings.json").exists()
    store = SettingsStore(root)
    s = store.load()
    if new:
        s.send_to_printer = True
        s.defaults.wait_for_monitor = True
        s.defaults.pause_first_sweep = "off"
        s.defaults.floor_calibrated = True
        s.defaults.loops = 5
    s.printers = [PrinterConfig(name=name, host="127.0.0.1", serial=p.serial, access_code=p.access_code,
                                model_id="N7", snapshot_url=p.snapshot_url, verify=VerifyConfig(settle_s=0.5))
                  for name, p in zip(NAMES, farm, strict=False)]
    store.save(s)
    for name in NAMES[:len(farm)]:
        refs = BedVerifier(root / "references" / name, None)
        if not refs.references:
            refs.add_reference(plate_picture())
    return new


def add_spools(root: Path) -> None:
    """Spools with the same RFID tags as the pretend AMS trays, so they link themselves, as Bambu spools do."""
    from proofloop.core.paths import PRINTERS_DB
    from proofloop.monitor.filament import Inventory, Spool
    from proofloop.monitor.store import Store

    inventory = Inventory(Store(root / PRINTERS_DB))
    if inventory.all():
        return
    for i, (material, color) in enumerate(COLORS):
        inventory.save(Spool(name="PLA Basic", brand="Bambu Lab", material=material, color="#" + color,
                                 price=19.99, net_g=1000, remaining_g=820 - 90 * i, tare_g=250,
                                 tag=f"{0x50E7E4D0 + i:08X}" + "0" * 24, preset="Bambu PLA Basic @BBL P2S"))
    inventory.save(Spool(name="PETG HF", brand="Bambu Lab", material="PETG", color="#1F1F1F", price=21.99,
                             net_g=1000, remaining_g=140, tare_g=250, low_g=200))


def start_a_run(port: int, plate: Path, parts: int = 60) -> None:
    """Plan a run over the three printers, keep it as a queue and send each printer its first job, the way
    you would in the window."""
    base = f"http://127.0.0.1:{port}"

    def call(path, body=None, ctype="application/json"):
        req = urllib.request.Request(base + path, data=body, headers={"Content-Type": ctype} if body else {})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"null")

    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{plate.name}\"\r\n\r\n"
            .encode() + plate.read_bytes() + f"\r\n--{boundary}--\r\n".encode())
    token = call("/api/plates", body, f"multipart/form-data; boundary={boundary}")["token"]
    run = call("/api/queue", json.dumps({"token": token, "parts": parts, "settings": {"loops": 5}}).encode())
    first: dict[str, dict] = {}
    for job in run["jobs"]:
        first.setdefault(job["printer"], job)
    for job in first.values():
        call(f"/api/queue/{run['id']}/{job['id']}/send", json.dumps({"confirm": job["printer"]}).encode())


def example_plate() -> Path | None:
    from proofloop.app.autostart import repo_root

    for root in (repo_root(), Path.cwd()):
        if root and (root / "examples" / "gcode" / "bracket_plate.gcode.3mf").is_file():
            return root / "examples" / "gcode" / "bracket_plate.gcode.3mf"
    return None


def run(port: int = PORT, window: bool = True, fresh: bool = False, open_window=None) -> int:
    real = home()
    farm = start_printers()
    os.environ["PROOFLOOP_HOME"] = str(real / "try")
    os.environ["PROOFLOOP_PRETEND"] = "1"
    from proofloop.app.service import Service

    try:
        new = prepare(farm, fresh)
        add_spools(folder())
        svc = Service(port, link_factory=link_factory(farm))
        svc.start()
        url = f"http://127.0.0.1:{port}/"
        print(f"ProofLoop with three pretend printers at {url}  (Ctrl+C to stop)")
        print(f"Its data is kept apart from yours, in {folder()}. Start over with: proofloop try --fresh")
        end = time.time() + 20
        while time.time() < end and not all(w.link.state.online for w in svc.shell.monitor.watches.values()):
            time.sleep(0.1)
        plate = example_plate()
        if new and plate:
            try:
                start_a_run(port, plate)
                print("Started a run of 60 brackets over the three printers: watch it under Printers › Overview.")
            except Exception as e:  # noqa: BLE001 - the farm still works; you can start one yourself
                log.warning("couldn't start the example run: %s", e)
        if window and open_window:
            open_window(url)
        try:
            svc.wait()
        except KeyboardInterrupt:
            svc.stop("Ctrl+C")
    finally:
        for p in farm:
            p.close()
    return 0
