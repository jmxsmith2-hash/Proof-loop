"""The first-run guide: the few things to do before a farm runs unattended, each with where to do it.

Unlike the setup check (``doctor.py``), which probes the network and can take seconds, these steps
are read from what the running ProofLoop already knows, so the window can tick them off live while
you work through them.
"""

from __future__ import annotations

from typing import Any


def _step(sid: str, title: str, done: bool, detail: str, go: dict[str, str] | None = None,
          optional: bool = False) -> dict[str, Any]:
    return {"id": sid, "title": title, "done": bool(done), "detail": detail, "go": go, "optional": optional}


def steps(shell) -> list[dict[str, Any]]:
    """The steps in order. ``go`` says where to do it: a tab, a section in it, and what to scroll to;
    or ``{"call": "autostart"}`` for something the window does in one click."""
    from proofloop.app import autostart
    from proofloop.loopfiles.profiles import ProfileStore

    mon = shell.monitor
    st = mon.settings
    printers = st.printers
    watches = [mon.watches[p.name] for p in printers if p.name in mon.watches]
    online = [w for w in watches if w.link.state.online]
    profiles = ProfileStore(mon.data_dir / "profiles")
    no_profile = sorted({p.model_id for p in printers if not profiles.for_printer(p.model_id)})
    refs = mon.data_dir / "references"
    no_pictures = [p.name for p in printers if not (refs / p.name).is_dir() or not any((refs / p.name).glob("*.jpg"))]
    monitor = {"tab": "printers", "section": "monitor"}
    out = [
        _step("printer", "Add your printer", bool(printers),
              "On the printer, turn on LAN-only mode and Developer Mode, and note its IP address, serial number and "
              "access code. Then add it here, or let Find printers fill most of it in.",
              {**monitor, "anchor": "addPrinter"}),
        _step("connected", "Connect to it", bool(printers) and len(online) == len(printers),
              "ProofLoop talks to the printer over your network. If it doesn't connect, check the access code (it "
              "changes each time you switch LAN-only or Developer Mode) and that both are on.",
              {**monitor, "anchor": "printerCards"}),
        _step("pictures", "Take pictures of the empty plate", bool(printers) and not no_pictures,
              "The plate check compares the camera with these. With the plate empty, press Save empty-plate "
              "picture; a few help (door open and shut, light on and off)."
              + (f" Still needed for: {', '.join(no_pictures)}." if printers and no_pictures else ""),
              {**monitor, "anchor": "printerCards"}),
        _step("profile", "Pick the hardware for your kit", bool(printers) and not no_profile,
              "The P2S with FarmLoop Stage 2 or the pusher alone is built in. Other printers learn your kit's moves "
              "from one of your FarmLoop files.",
              {"tab": "loopfiles", "section": "profiles", "anchor": "importForm"}),
        _step("floor", "Measure the sweep floor", st.defaults.floor_calibrated,
              "A short test print finds the lowest height your pusher can sweep without touching the plate, so short "
              "parts get pushed off. Once per printer, and again after changing the pusher.",
              {"tab": "loopfiles", "section": "calibrate", "anchor": "calFile"}),
        _step("notifications", "Get alerts on your phone", bool(st.notifications),
              "ntfy, Discord or a webhook: you hear when parts are left on the plate, a print fails or ProofLoop "
              "stops.", {**monitor, "anchor": "notifyForm"}),
        _step("autostart", "Start ProofLoop when you log in", autostart.enabled(),
              "So the printers are watched again after a restart, with nobody opening ProofLoop.",
              {"call": "autostart"}),
    ]
    try:
        from proofloop.check.slicer import find_slicer

        found = find_slicer()
        slicer = bool(found and found.profile_dirs)
    except Exception:  # noqa: BLE001 - a broken slicer install just counts as missing
        slicer = False
    out.append(_step("slicer", "Install Bambu Studio for print-ready verdicts", slicer,
                     "Check can then slice your designs itself and say whether they're ready to print. Open it once "
                     "after installing, so it downloads its printer presets.", None, optional=True))
    return out


def summary(shell) -> dict[str, Any]:
    all_steps = steps(shell)
    required = [s for s in all_steps if not s["optional"]]
    return {"steps": all_steps, "done": sum(s["done"] for s in required), "total": len(required),
            "complete": all(s["done"] for s in required)}
