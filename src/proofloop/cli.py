"""``proofloop``: the one command.

    proofloop                      open the window (ProofLoop starts in the background if it isn't running)
    proofloop check FILE [...]     check a CAD file or sliced G-code (loop files get the loop-file checks too)
    proofloop check COMMAND ...    design, gcode, prepare (slice), learn, printed, history, review, new
    proofloop loop PLATE [...]     make a loop file from a sliced plate; also: check, cost, calibrate, profiles
    proofloop printers COMMAND     history, spools, serve (Loop files & Printers alone, e.g. in Docker)
    proofloop service              run in the background with a tray icon (what the shortcut and login start)
    proofloop app                  run in this terminal instead, until Ctrl+C
    proofloop stop | status        quit the background ProofLoop / is it running?
    proofloop autostart on|off     start in the background when you log in
    proofloop phone on|off|forget  phone access on your network
    proofloop doctor               check the setup (--slice-test, --send-test)
    proofloop update               get the newest ProofLoop (--check to only look)
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from proofloop import __version__
from proofloop.app.config import DEFAULT_PORT
from proofloop.app.shell import HEADER
from proofloop.core.paths import home


def _ask(port: int, path: str, body: dict | None = None, timeout: float = 3.0) -> dict | None:
    """Talk to the running ProofLoop on this computer. None when nothing answers."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={HEADER: "1"})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read() or b"{}").get("detail", str(e))}
        except ValueError:
            return {"error": str(e)}
    except (OSError, ValueError):
        return None


def running(port: int = DEFAULT_PORT) -> bool:
    """Is a ProofLoop already serving this port? (Double-clicking the shortcut twice just shows it.)"""
    r = _ask(port, "/proofloop/ping", timeout=1.5)
    return bool(r and "proofloop" in r)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":  # as the server binds: closed connections lingering don't count
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _open_window(url: str) -> None:
    from proofloop.app.window import open_window

    open_window(url, home() / "window")  # its own window in Edge or Chrome, else a browser tab


def _log_to_file() -> Path:
    """The background service has no console: everything goes to ~/.proofloop/proofloop.log."""
    path = home() / "proofloop.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    stream = open(path.with_suffix(".out"), "a", buffering=1, encoding="utf-8")  # noqa: SIM115 - lives as long as the process
    sys.stdout = sys.stderr = stream  # pythonw has none; uvicorn and print() need somewhere to write
    return path


def _spawn_service(port: int) -> None:
    from proofloop.app.autostart import background_python, repo_root

    cmd = [str(background_python()), "-m", "proofloop", "service", "--port", str(port)]
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "cwd": str(repo_root() or Path.cwd()), "close_fds": True}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | \
            subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)


def cmd_open(a) -> int:
    url = f"http://127.0.0.1:{a.port}/"
    if not running(a.port):
        if not _port_free(a.port):
            print(f"Port {a.port} is used by another program. Try: proofloop --port {a.port + 1}", file=sys.stderr)
            return 2
        print("Starting ProofLoop in the background…")
        _spawn_service(a.port)
        end = time.time() + 60
        while not running(a.port):
            if time.time() > end:
                print(f"ProofLoop didn't start. See {home() / 'proofloop.log'}", file=sys.stderr)
                return 2
            time.sleep(0.4)
    if a.window:
        _open_window(url)
    print(f"ProofLoop is running at {url}. Closing the window keeps it watching your printers; quit it from the "
          "tray icon or with: proofloop stop")
    return 0


def cmd_service(a) -> int:
    if running(a.port):
        return 0  # already running (e.g. started at login and again from the shortcut)
    _log_to_file()
    from proofloop.app.service import Service

    try:
        svc = Service(a.port)
        svc.start()
    except Exception:  # noqa: BLE001
        logging.exception("ProofLoop couldn't start")
        return 2
    if a.tray:
        from proofloop.app import tray

        if tray.run(svc, _open_window):
            return 0
    try:
        svc.wait()
    except KeyboardInterrupt:
        svc.stop("Ctrl+C")
    return 0


def cmd_app(a) -> int:
    """Run in this terminal (no tray): for testing, or when you want to see what it's doing."""
    url = f"http://127.0.0.1:{a.port}/"
    if running(a.port):
        print(f"ProofLoop is already open at {url}")
        if a.window:
            _open_window(url)
        return 0
    if not _port_free(a.port):
        print(f"Port {a.port} is used by another program. Try: proofloop app --port {a.port + 1}", file=sys.stderr)
        return 2
    from proofloop.app.service import Service

    try:
        svc = Service(a.port)
        svc.start()
    except RuntimeError as e:
        print(f"ProofLoop can't start: {e}", file=sys.stderr)
        return 2
    print(f"ProofLoop {__version__} at {url}  (Ctrl+C to stop)")
    if a.window:
        _open_window(url)
    try:
        svc.wait()
    except KeyboardInterrupt:
        svc.stop("Ctrl+C in its terminal")
    return 0


def cmd_stop(a) -> int:
    r = _ask(a.port, "/proofloop/quit", {"reason": "proofloop stop"})
    if r is None:
        print("ProofLoop isn't running.")
        return 0
    if "error" in r:
        print(r["error"], file=sys.stderr)
        return 2
    marker = home() / "running.json"
    end = time.time() + 40  # until it has finished stopping (it removes its marker last)
    while (running(a.port) or marker.exists()) and time.time() < end:
        time.sleep(0.3)
    print("ProofLoop stopped: your printers are not being watched.")
    return 0


def cmd_status(a) -> int:
    from proofloop.app import autostart

    r = _ask(a.port, "/proofloop/status")
    if not r:
        print("ProofLoop isn't running.")
    else:
        print(r.get("summary") or "ProofLoop is running")
        ph = r.get("phone") or {}
        print(f"phone access: {'on at ' + ph['url'] if ph.get('on') else 'off'}")
    print(f"starts when you log in: {'yes' if autostart.enabled() else 'no'}")
    return 0 if r else 1


def cmd_autostart(a) -> int:
    from proofloop.app import autostart

    if a.what == "on":
        print(f"ProofLoop will start in the background when you log in ({autostart.enable()}).")
    elif a.what == "off":
        autostart.disable()
        print("ProofLoop won't start when you log in.")
    else:
        print("on" if autostart.enabled() else "off")
    return 0


def cmd_phone(a) -> int:
    body = {"status": {}, "on": {"on": True}, "off": {"on": False}, "forget": {"forget": True, "on": True}}[a.what]
    r = _ask(a.port, "/proofloop/phone" if body else "/proofloop/status", body or None, timeout=15)
    if r is None:
        print("ProofLoop isn't running: start it first (proofloop).", file=sys.stderr)
        return 2
    if "error" in r:
        print(r["error"], file=sys.stderr)
        return 2
    ph = r.get("phone") or {}
    if not ph.get("on"):
        print("Phone access is off." + (f" ({ph['error']})" if ph.get("error") else ""))
        return 0
    print(f"Phone access is on. On your phone (same Wi-Fi), open {ph['url']} and type the code {ph['code']},")
    print("or scan this:")
    try:
        import qrcode

        q = qrcode.QRCode(border=1)
        q.add_data(ph["pair_url"])
        q.print_ascii(invert=True)
    except Exception:  # noqa: BLE001
        print(ph["pair_url"])
    return 0


def cmd_doctor(a) -> int:
    from proofloop.app.doctor import BAD, checks, text

    results = checks(None, slice_test=a.slice_test, send_test=a.send_test, port=a.port)
    print(text(results))
    return 1 if any(r["level"] == BAD for r in results) else 0


def cmd_update(a) -> int:
    from proofloop.app.update import update

    was_running = running(a.port) and not a.check
    if was_running:
        print("Stopping ProofLoop for the update…")
        cmd_stop(a)
    code = update(check_only=a.check)
    if was_running:
        print("Starting ProofLoop again…")
        _spawn_service(a.port)
    return code


def _typer(app, args: list[str], prog: str) -> int:
    try:
        app(args=args, prog_name=prog)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


CHECK_COMMANDS = ("design", "gcode", "prepare", "learn", "printed", "history", "review", "new")


def cmd_check(args: list[str]) -> int:
    """``proofloop check FILE`` picks the design or G-code check; ``proofloop check COMMAND`` runs that one."""
    from proofloop.check.cli import app

    if args and not args[0].startswith("-") and args[0] not in CHECK_COMMANDS:
        from proofloop.app.checks import file_kind

        kind = file_kind(Path(args[0]).name)
        if kind is None:
            print(f"{args[0]}: give a CAD file (STL, 3MF, OBJ, STEP...) or sliced G-code (.gcode, .gcode.3mf)",
                  file=sys.stderr)
            return 2
        args = ["gcode" if kind == "gcode" else "design", *args]
    return _typer(app, args, "proofloop check")


def _server_opts(p: argparse.ArgumentParser, window: bool = True) -> None:
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port on this computer (default {DEFAULT_PORT})")
    if window:
        p.add_argument("--no-window", dest="window", action="store_false", help="don't open the window")


def _utf8_output() -> None:
    """Windows consoles and pipes often use cp1252, which has no ✓ or ✖: write UTF-8, never crash on a character."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # None under pythonw, or not a text stream
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    try:
        return _main(argv)
    except BrokenPipeError:  # e.g. `proofloop status | head -1`
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
        return 0


def _main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # The four parts have commands of their own, with every option.
    if argv and argv[0] == "check":
        return cmd_check(argv[1:])
    if argv and argv[0] == "loop":
        from proofloop.loopfiles.cli import main as loop_main

        return loop_main(argv[1:])
    if argv and argv[0] == "printers":
        from proofloop.monitor.cli import main as printers_main

        return printers_main(argv[1:])

    ap = argparse.ArgumentParser(
        prog="proofloop", description="Check designs, make loop files and watch your printers",
        epilog="Each part has its own commands: proofloop check|loop|printers --help",
    )
    ap.add_argument("--version", action="version", version=f"proofloop {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    _server_opts(sub.add_parser("open", help="open the window, starting ProofLoop if needed (the default)"))
    p = sub.add_parser("service", help="run in the background with a tray icon")
    _server_opts(p, window=False)
    p.add_argument("--no-tray", dest="tray", action="store_false", help="no tray icon")
    _server_opts(sub.add_parser("app", help="run in this terminal until Ctrl+C (no tray)"))
    for name, text in (("stop", "quit the background ProofLoop"), ("status", "is ProofLoop running?")):
        sub.add_parser(name, help=text).add_argument("--port", type=int, default=DEFAULT_PORT)
    p = sub.add_parser("autostart", help="start in the background when you log in")
    p.add_argument("what", choices=["on", "off", "status"], nargs="?", default="status")
    p = sub.add_parser("phone", help="phone access on your network")
    p.add_argument("what", choices=["on", "off", "forget", "status"], nargs="?", default="status")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p = sub.add_parser("doctor", help="check that everything ProofLoop needs is there and working")
    p.add_argument("--slice-test", action="store_true", help="also slice a small example part with your slicer")
    p.add_argument("--send-test", action="store_true", help="also send a test notice to your notification targets")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p = sub.add_parser("update", help="get the newest ProofLoop from GitHub")
    p.add_argument("--check", action="store_true", help="only say whether there is an update")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("check", help="check a CAD file or sliced G-code before you print")
    sub.add_parser("loop", help="make a loop file from a sliced plate, or check or cost one")
    sub.add_parser("printers", help="print history and filament; serve Loop files & Printers alone")

    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        argv = ["open", *argv]  # plain `proofloop` (with options) opens the window
    a = ap.parse_args(argv)
    return {"open": cmd_open, "service": cmd_service, "app": cmd_app, "stop": cmd_stop, "status": cmd_status,
            "autostart": cmd_autostart, "phone": cmd_phone, "doctor": cmd_doctor, "update": cmd_update}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
