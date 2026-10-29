"""``proofloop``: one command for synthux and ClearLoop.

    proofloop                      open the ProofLoop window (same as ``proofloop app``)
    proofloop check FILE [...]     check a CAD file or sliced G-code (synthux; loop files get ClearLoop's checks too)
    proofloop loop PLATE [...]     make a loop file from a sliced plate (ClearLoop's ``make``)
    proofloop synthux ...          any synthux command
    proofloop clearloop ...        any ClearLoop command
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .shell import SETUP_HINT, installed

DEFAULT_PORT = 8600
WINDOW_PROFILE = Path.home() / ".proofloop" / "window"


def _running(port: int) -> bool:
    """Is a ProofLoop already serving this port? (Double-clicking the shortcut twice just shows it.)"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/proofloop/ping", timeout=1.5) as r:
            return "proofloop" in json.loads(r.read() or b"{}")
    except (OSError, ValueError):
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _open_window(url: str) -> None:
    if installed("synthux"):
        from synthux.app.server import open_window

        WINDOW_PROFILE.mkdir(parents=True, exist_ok=True)
        open_window(url, WINDOW_PROFILE)  # its own window in Edge or Chrome, else a browser tab
    else:
        webbrowser.open(url)


def cmd_app(a) -> int:
    url = f"http://127.0.0.1:{a.port}/"
    if _running(a.port):
        print(f"ProofLoop is already open at {url}")
        if a.window:
            _open_window(url)
        return 0
    if not _port_free(a.port):
        print(f"Port {a.port} is used by another program. Try: proofloop app --port {a.port + 1}", file=sys.stderr)
        return 2
    import uvicorn

    from .shell import Shell

    try:
        shell = Shell(a.port, clearloop_data=a.data, synthux_config=a.config)
    except RuntimeError as e:
        print(f"ProofLoop can't start: {e}", file=sys.stderr)
        return 2
    parts = [n for n, on in (("synthux", shell.synthux), ("ClearLoop", shell.clearloop)) if on]
    print(f"ProofLoop {__version__} ({' + '.join(parts)}) at {url}  (close this window or press Ctrl+C to stop)")
    if shell.clearloop and not shell.synthux:
        print(f"synthux isn't installed, so only ClearLoop is shown: {SETUP_HINT}")
    if shell.synthux and not shell.clearloop:
        print(f"ClearLoop isn't installed, so only synthux is shown: {SETUP_HINT}")
    if a.window:
        _open_window(url)
    try:
        uvicorn.run(shell.api, host="127.0.0.1", port=a.port, log_level="warning")
    finally:
        shell.close()
    return 0


def _synthux(args: list[str]) -> int:
    if not installed("synthux"):
        print(f"synthux isn't installed: {SETUP_HINT}", file=sys.stderr)
        return 2
    from synthux.cli import app

    try:
        app(args=args, prog_name="proofloop synthux")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


def _clearloop(args: list[str]) -> int:
    if not installed("clearloop"):
        print(f"ClearLoop isn't installed: {SETUP_HINT}", file=sys.stderr)
        return 2
    from clearloop.cli import main as clearloop_main

    return clearloop_main(args)


def cmd_check(a, rest: list[str]) -> int:
    if not installed("synthux"):
        if a.file.lower().endswith(".3mf"):
            return _clearloop(["check", a.file, *rest])  # ClearLoop can still check a loop file
        print(f"synthux isn't installed: {SETUP_HINT}", file=sys.stderr)
        return 2
    from synthux.app.server import file_kind

    kind = file_kind(Path(a.file).name)
    if kind is None:
        print(f"{a.file}: give a CAD file (STL, 3MF, OBJ, STEP...) or sliced G-code (.gcode, .gcode.3mf)",
              file=sys.stderr)
        return 2
    return _synthux(["physical", "gcode" if kind == "gcode" else "check", a.file, *rest])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Pass-throughs keep every option of the two tools, untouched.
    if argv and argv[0] == "synthux":
        return _synthux(argv[1:])
    if argv and argv[0] == "clearloop":
        return _clearloop(argv[1:])
    if argv and argv[0] == "loop":
        return _clearloop(["make", *argv[1:]])

    ap = argparse.ArgumentParser(
        prog="proofloop", description="synthux and ClearLoop in one window",
        epilog="Also: proofloop loop PLATE [options] (ClearLoop's make), proofloop synthux ..., proofloop clearloop ...",
    )
    ap.add_argument("--version", action="version", version=f"proofloop {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("app", help="open the ProofLoop window (the default)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port on this computer (default {DEFAULT_PORT})")
    p.add_argument("--no-window", dest="window", action="store_false", help="don't open the window")
    p.add_argument("--data", help="ClearLoop's settings, profiles and logs (default ~/.clearloop)")
    p.add_argument("--config", help="synthux's config folder (default: found from here)")
    p = sub.add_parser("check", help="check a CAD file or sliced G-code (any synthux option after the file)")
    p.add_argument("file")
    sub.add_parser("loop", help="make a loop file from a sliced plate (ClearLoop's make options)")
    sub.add_parser("synthux", help="run a synthux command")
    sub.add_parser("clearloop", help="run a ClearLoop command")

    a, rest = ap.parse_known_args(argv or ["app"])
    if a.cmd == "check":
        return cmd_check(a, rest)
    if rest:
        ap.error(f"unrecognised arguments: {' '.join(rest)}")
    return cmd_app(a)


if __name__ == "__main__":
    raise SystemExit(main())
