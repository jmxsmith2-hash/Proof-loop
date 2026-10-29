"""The ProofLoop icon next to the clock: open the window, phone access, start at login, quit.

Needs pystray (installed on Windows and macOS). Where there is no tray (or no desktop), the
service runs without one and the window does everything.
"""

from __future__ import annotations

import logging
import webbrowser

from . import autostart

log = logging.getLogger(__name__)


def icon_image(size: int = 64):
    """A loop arrow with a tick, drawn rather than shipped as a file."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(4, size // 10)
    pad = w
    d.ellipse((0, 0, size - 1, size - 1), fill=(42, 120, 214, 255))
    d.arc((pad * 2, pad * 2, size - pad * 2, size - pad * 2), start=-60, end=250, fill="white", width=w)
    tip = (size * 0.80, size * 0.30)
    d.polygon([(tip[0], tip[1] - w * 1.4), (tip[0] + w * 1.3, tip[1] + w * 0.9), (tip[0] - w * 1.5, tip[1] + w * 0.6)],
              fill="white")
    d.line([(size * 0.36, size * 0.52), (size * 0.47, size * 0.63), (size * 0.66, size * 0.41)], fill="white",
           width=w, joint="curve")
    return img


def run(service, open_window) -> bool:
    """Show the tray icon until ProofLoop quits. False when there is no tray to show it in."""
    try:
        import pystray
    except Exception:  # noqa: BLE001 - not installed, or no desktop to draw on
        log.info("no system tray here; running without an icon")
        return False

    def open_(icon=None, item=None):
        open_window(f"http://127.0.0.1:{service.port}/")

    def setup_check(icon=None, item=None):
        webbrowser.open(f"http://127.0.0.1:{service.port}/proofloop/doctor")

    def toggle_phone(icon, item):
        if service.phone.on:
            service.phone.stop()
            service.settings.phone = False
        else:
            try:
                service.phone.start()
                service.settings.phone = True
            except RuntimeError as e:
                service.notify("warning", f"phone access couldn't start: {e}")
        service.settings.save()
        icon.update_menu()

    def toggle_autostart(icon, item):
        try:
            autostart.disable() if autostart.enabled() else autostart.enable()
        except Exception:  # noqa: BLE001
            log.exception("autostart")
        icon.update_menu()

    def quit_(icon, item):
        service.stop("you chose Quit")

    menu = pystray.Menu(
        pystray.MenuItem("Open ProofLoop", open_, default=True),
        pystray.MenuItem(lambda item: service.summary(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Phone access", toggle_phone, checked=lambda item: bool(service.phone and service.phone.on),
                         enabled=lambda item: service.phone is not None),
        pystray.MenuItem("Start when I log in", toggle_autostart, checked=lambda item: autostart.enabled()),
        pystray.MenuItem("Check my setup", setup_check),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit (stops watching printers)", quit_),
    )
    try:
        icon = pystray.Icon("ProofLoop", icon_image(), service.summary(), menu)
    except Exception:  # noqa: BLE001
        log.exception("tray icon")
        return False

    def refresh():
        icon.title = service.summary()
        icon.update_menu()

    service.on_stop.append(icon.stop)
    service.on_beat.append(refresh)
    icon.run()  # until Quit, or the service stops for another reason
    service.stop("the tray icon closed")
    service.wait()
    return True
