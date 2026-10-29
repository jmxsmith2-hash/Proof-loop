"""``proofloop doctor``: is everything ProofLoop needs actually there and working, on this computer?

Each check says what it found and, when something is missing, exactly what to do about it. With
``--slice-test`` it slices a small example part with your real slicer, and with ``--send-test`` it
sends a test notice to your notification targets.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import time
from pathlib import Path

from proofloop import __version__
from proofloop.app import autostart
from proofloop.app.config import DEFAULT_PORT, Settings
from proofloop.core.paths import home

OK, WARN, BAD, INFO = "ok", "warning", "problem", "info"
ICON = {OK: "✓", WARN: "!", BAD: "✖", INFO: "i"}
MQTT_PORT = 8883  # Bambu printers' local MQTT (LAN mode)


def _reachable(host: str, port: int, timeout: float = 2.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def checks(shell=None, slice_test: bool = False, send_test: bool = False, port: int = DEFAULT_PORT) -> list[dict]:
    out: list[dict] = []

    def add(level: str, title: str, detail: str = "") -> None:
        out.append({"level": level, "title": title, "detail": detail})

    add(OK, f"ProofLoop {__version__} on Python {sys.version.split()[0]}", sys.executable)
    add(OK, "Your settings and data", str(home()))
    _check_checks(add, slice_test)  # Check: CAD, G-code and slicing
    _loop_checks(add, shell, send_test)  # Loop files and Printers: printers, plate check, notifications

    # ---- running in the background --------------------------------------------------
    if autostart.enabled():
        add(OK, "Starts when you log in", "so the printers are watched again after a restart")
    else:
        add(WARN, "Doesn't start when you log in",
            "After a restart nothing watches the printers until you open ProofLoop. Turn on "
            "\"Start when I log in\" in the tray menu, or run: proofloop autostart on")
    if shell is None:
        from proofloop.cli import running

        add(OK if running(port) else INFO,
            "ProofLoop is running in the background" if running(port) else "ProofLoop isn't running right now",
            "" if running(port) else "Double-click the ProofLoop shortcut to start it.")
    s = Settings.load()
    phone = getattr(getattr(shell, "service", None), "phone", None)
    if phone and phone.on:
        add(OK, "Phone access is on", phone.url())
    elif s.phone:
        add(WARN, "Phone access is switched on but not listening", (phone.error if phone else "") or
            "ProofLoop isn't running, or another program uses the port")
    else:
        add(INFO, "Phone access is off", "Turn it on with the Phone button in the ProofLoop window.")
    return out


def _check_checks(add, slice_test: bool) -> None:
    from proofloop.core.config import find_config_dir, load_settings

    cfg = find_config_dir()
    own = sorted(p.name for p in cfg.glob("*.yaml")) if cfg.is_dir() else []
    add(INFO, "Advanced settings: " + (", ".join(own) + " are yours" if own else "all built-in"),
        f"To change one, put the file (or just the setting, for settings.yaml) in {cfg}.")
    from proofloop.check.slicer import PresetLibrary, find_slicer

    try:
        found = find_slicer()
    except Exception as e:  # noqa: BLE001
        found = None
        add(BAD, "The slicer couldn't be checked", str(e))
    if found is None:
        add(WARN, "No Bambu Studio or OrcaSlicer found",
            "CAD files are checked but not sliced, so there's no print-ready verdict. Install Bambu Studio "
            "(free) in its usual place, or set PROOFLOOP_SLICER to the program.")
    elif not found.profile_dirs:
        add(BAD, f"{found.label} found, but not its printer presets",
            f"{found.exe}. Open {found.label} once so it downloads them, or set PROOFLOOP_SLICER_PROFILES to the "
            "folder that holds BBL/.")
    else:
        add(OK, f"{found.label} found", str(found.exe))
        try:
            from proofloop.check.cad.load import load_profiles

            pr, mat, _ = load_profiles(cfg, None, None)
            lib = PresetLibrary(found.profile_dirs)
            machine = lib.machine((pr.get("names") or [pr["name"]])[0], float(pr["nozzle"]))
            add(OK, f"Slicer presets for your printer: {machine}",
                f"{lib.process(machine, 0.2)} · {lib.filament(machine, mat['name'])}")
        except Exception as e:  # noqa: BLE001
            add(BAD, "The slicer has no presets for your printer", str(e))
        if slice_test:
            _slice_test(add, load_settings(cfg))


def _slice_test(add, settings) -> None:
    from proofloop.check.prepare import prepare
    from proofloop.core.db import Database

    root = autostart.repo_root()
    part = (root / "examples" / "cad" / "block_control.stl") if root else None
    if not part or not part.is_file():
        add(INFO, "Slice test skipped", "the example part isn't here (ProofLoop isn't running from its folder)")
        return
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="proofloop-slice-") as tmp:
        db = Database(Path(tmp) / "slice-test.sqlite")
        try:
            r = prepare(settings, db, part, out=Path(tmp) / "out", personas=2)
            add(OK, f"Slice test: {part.name} sliced in {time.time() - t0:.0f} s",
                f"{r['slicer']['name']}, {r['layers']} layers, {r['grams']} g; "
                + ("ready to print" if r["ready"] else f"{r['blocking']} problems found (expected for a test part)"))
        except Exception as e:  # noqa: BLE001
            add(BAD, "Slice test failed", str(e)[-900:])
        finally:
            db.close()


def _loop_checks(add, shell, send_test: bool) -> None:
    from proofloop.core.notify import Notifier
    from proofloop.core.printers import model_name
    from proofloop.core.settings import SettingsStore
    from proofloop.loopfiles.profiles import ProfileStore

    app = getattr(shell, "monitor", None)
    data = Path(app.data_dir) if app else home()
    st = app.settings if app else SettingsStore(data).load()
    if not st.printers:
        add(WARN, "No printers added yet",
            "Add them under Printers › Monitor, so ProofLoop can watch them and learn what fails.")
    profiles = ProfileStore(data / "profiles")
    for p in st.printers:
        w = app.watches.get(p.name) if app else None
        if w and w.link.state.online:
            add(OK, f"{p.name}: connected", f"{model_name(p.model_id)} at {p.host}")
        elif _reachable(p.host, MQTT_PORT):
            add(OK if not app else WARN, f"{p.name}: reachable at {p.host}",
                "" if not app else "but not connected yet: check the access code and serial number")
        else:
            add(BAD, f"{p.name}: can't reach {p.host}",
                "Is it on, on the same network, with LAN mode (or Developer mode) on? Check the IP address on the "
                "printer's screen.")
        if not profiles.for_printer(p.model_id):
            add(BAD, f"{p.name}: no hardware profile for the {model_name(p.model_id)}",
                "Import one from a FarmLoop file under Loop files › Hardware before making loop files for it.")
        refs = data / "references" / p.name
        n = len(list(refs.glob("*.jpg"))) if refs.is_dir() else 0
        add(OK if n else WARN, f"{p.name}: {n} empty-plate picture{'s' if n != 1 else ''}",
            "" if n else "The plate check needs them: Printers › Monitor › take a picture of the empty plate.")
    if st.defaults.floor_calibrated:
        add(OK, f"Sweep floor calibrated: Z{st.defaults.floor_z:g}")
    else:
        add(WARN, "Sweep floor not calibrated",
            "Loop files start at Z2 until then. Run the sweep-floor test under Loop files › Calibrate once per printer.")
    mode = st.defaults.pause_first_sweep
    add(INFO, f"Safety pause before the first sweep: {mode}",
        "auto = on until a loop has swept cleanly on one of your printers")
    if st.notifications:
        add(OK, f"{len(st.notifications)} notification target{'s' if len(st.notifications) != 1 else ''} set up",
            ", ".join(n.kind for n in st.notifications))
        if send_test:
            sent = Notifier(st.notifications).send(
                {"ts": time.time(), "printer": None, "level": "error", "kind": "test",
                 "message": "Test notice from proofloop doctor: notifications work."}, None, wait=True)
            n = len(st.notifications)
            add(OK if sent == n else BAD, f"Test notice: {sent} of {n} target{'s' if n != 1 else ''} took it",
                "check your phone" if sent else "check the URLs under Printers › Monitor")
    else:
        add(WARN, "No notifications set up",
            "You won't hear about failures or stops when you're away. Add ntfy, Discord or a webhook in "
            "Printers › Monitor.")


def text(results: list[dict]) -> str:
    lines = []
    for r in results:
        lines.append(f"{ICON[r['level']]} {r['title']}")
        if r["detail"]:
            lines.append(f"    {r['detail']}")
    return "\n".join(lines)


def html_page(results: list[dict]) -> str:
    import html as h

    from proofloop.core import theme

    css = {OK: "ok", WARN: "warning", BAD: "danger", INFO: "info"}
    rows = "".join(
        f'<li><span class="ic {css[r["level"]]}" aria-label="{r["level"]}">{ICON[r["level"]]}</span>'
        f'<div><b>{h.escape(r["title"])}</b>'
        + (f'<div class="hint">{h.escape(r["detail"])}</div>' if r["detail"] else "") + "</div></li>" for r in results)
    bad = sum(r["level"] == BAD for r in results)
    warn = sum(r["level"] == WARN for r in results)
    parts = [f"{bad} problem{'s' if bad != 1 else ''}"] * bool(bad) + [f"{warn} thing{'s' if warn != 1 else ''} to look at"] * bool(warn)
    head = " and ".join(parts).capitalize() + "." if parts else "Everything looks ready."
    chip = "ok" if not (bad or warn) else "bad" if bad else "warn"
    return theme.inline(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Setup check · ProofLoop</title>
<link rel="icon" href="__PROOFLOOP_FAVICON__"><style>/*__THEME__*/</style>
<style>main{{max-width:760px;padding-top:24px}}.brand{{margin-bottom:12px}}.hint{{word-break:break-word}}</style>
</head><body><main><div class="brand">__PROOFLOOP_ICON__ProofLoop</div>
<div class="spread"><h1>Setup check</h1><span class="chip {chip}">{h.escape(head)}</span></div>
<p class="sub">Run <code>proofloop doctor --slice-test --send-test</code> to also slice a test part with your slicer
and send a test notice.</p><div class="card"><ul class="list">{rows}</ul></div></main></body></html>""")
