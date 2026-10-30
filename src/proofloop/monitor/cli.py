"""``proofloop printers``: print history, filament spools, and serving Loop files & Printers on your network."""

from __future__ import annotations

import argparse
import logging
import sys
import time
import webbrowser

from proofloop import __version__
from proofloop.core.output import emit
from proofloop.core.paths import PRINTERS_DB, home
from proofloop.monitor.printer.ams import label as tray_label
from proofloop.monitor.service import App


def cmd_history(a) -> int:
    from proofloop.monitor.store import Store

    rows = Store(home() / PRINTERS_DB).outcomes(limit=a.limit)
    if a.json:
        emit(rows)
        return 0
    if not rows:
        print("No prints recorded yet: ProofLoop records every print on the printers it watches.")
        return 0
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
        loop = f" loop {r['loop_no']}" if r.get("loop_file") else ""
        layer = f" at layer {r['layer']}/{r['total_layers']}" if r["outcome"] != "finished" and r.get("layer") else ""
        hms = f"  HMS {', '.join(h['code'] for h in r['hms'])}" if r.get("hms") else ""
        eject = {"clear": "  plate empty", "parts_left": "  PARTS LEFT"}.get(r.get("eject") or "", "")
        print(f"  {when}  {r['printer']:10} {r['outcome']:9} {r['job']}{loop}{layer}{hms}{eject}")
    return 0


def cmd_spools(a) -> int:
    app = App(home(), connect=False)
    low = app.settings.filament.low_g
    spools = app.inventory.all(archived=a.all)
    if a.json:
        emit({"spools": [sp.as_dict(low) | {"forecast": app.inventory.forecast(sp)} for sp in spools],
              "summary": app.inventory.summary()})
        return 0
    if not spools:
        print("No spools yet: add them under Printers › Filament in the window.")
        return 0
    for sp in sorted(spools, key=lambda s: (s.material, s.label)):
        left = "unknown" if sp.remaining_g is None else f"{sp.remaining_g:.0f} g"
        where = f"{sp.printer} {tray_label(sp.tray)}" if sp.printer and sp.tray is not None else sp.location
        status = sp.status(low)
        flag = {"low": "  LOW", "empty": "  USED UP", "unknown": "  (weigh it)"}.get(status, "")
        print(f"  {sp.id}  {sp.material:6} {sp.color or '':8} {sp.label[:28]:28} {left:>9}  {where}{flag}")
    s = app.inventory.summary()
    print(f"\n  {s['spools']} spools, {s['on_hand_g'] / 1000:.2f} kg on hand; used {s['used_g']['7']:.0f} g in 7 days")
    return 0


def cmd_serve(a) -> int:
    """Loop files & Printers alone, e.g. in Docker or on an always-on computer, reachable on your network."""
    import uvicorn

    from proofloop.app.loop_server import create_app

    app = App(home())
    url = f"http://{'localhost' if a.host in ('0.0.0.0', '127.0.0.1') else a.host}:{a.port}"
    print(f"ProofLoop {__version__} (Loop files & Printers) at {url}  (data in {home()})")
    if a.host not in ("127.0.0.1", "localhost") and not app.settings.app_token:
        print("  Anyone on your network can use it. Set an access token in settings.json (app_token) to require one.")
    if a.browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - no browser (Docker, a server): fine
            pass
    try:
        uvicorn.run(create_app(app), host=a.host, port=a.port, log_level="warning")
    finally:
        app.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="proofloop printers", description="Your printers: print history, filament, "
                                 "and Loop files & Printers on its own")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("history", help="how recent prints ended (recorded while ProofLoop watches your printers)")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true", help="everything recorded, as JSON")
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("spools", help="list your filament spools and what's left on them")
    p.add_argument("--all", action="store_true", help="include archived spools")
    p.add_argument("--json", action="store_true", help="as JSON (for programs and assistants)")
    p.set_defaults(fn=cmd_spools)

    p = sub.add_parser("serve", help="serve Loop files & Printers alone (Docker, an always-on computer)")
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to reach it from other devices on your network")
    p.add_argument("--port", type=int, default=8484)
    p.add_argument("--browser", action="store_true", help="open it in your browser")
    p.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
