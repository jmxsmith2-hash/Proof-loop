"""Command-line interface. Most people use the app (``clearloop app``); everything is here too."""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from pathlib import Path

from . import __version__, printers, threemf
from .calibration import TESTS, build_test
from .config import Defaults, SettingsStore
from .gcode.processor import ProcessError, analyze, process
from .importer import ImportError_, import_profile
from .lint import lint
from .profiles import ProfileStore
from .service import loop_settings

DEFAULT_DATA = Path.home() / ".clearloop"
ICON = {"danger": "✖", "warning": "!", "info": "i", "ok": "✓"}


def _profiles(a) -> ProfileStore:
    return ProfileStore(Path(a.data) / "profiles")


def _defaults(a) -> Defaults:
    d = SettingsStore(a.data).load().defaults.model_dump()
    for key in ("profile_id", "loops", "release_temp", "floor_z", "natural_cooldown_min", "cooldown_fan"):
        v = getattr(a, key, None)
        if v is not None:
            d[key] = v
    if getattr(a, "no_bender", False):
        d["bender_fitted"] = False
    if getattr(a, "wait_for_monitor", False):
        d["wait_for_monitor"] = True
    return Defaults.model_validate(d)


def _pick_profile(a, sf: threemf.SlicedFile, d: Defaults):
    store = _profiles(a)
    if getattr(a, "profile_id", None):
        return store.get(a.profile_id)
    matching = store.for_printer(sf.info.printer_model_id)
    if not matching:
        raise SystemExit(
            f"no hardware profile for the {printers.model_name(sf.info.printer_model_id)} yet. Import one from a "
            "FarmLoop file: clearloop import-profile PLAIN.gcode.3mf FL_S1_PLAIN.gcode.3mf"
        )
    by_id = {p.id: p for p in matching}
    return by_id.get(d.profile_id, matching[0])


def cmd_make(a) -> int:
    sf = threemf.load(a.input, plate=a.plate)
    d = _defaults(a)
    prof = _pick_profile(a, sf, d)
    st = loop_settings(d)
    try:
        an = analyze(sf, prof, st)
        _print_analysis(an, prof)
        if not an.ok:
            return 2
        r = process(sf, prof, st)
    except ProcessError as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2
    out = Path(a.output) if a.output else Path(a.input).with_name(r.filename)
    out.write_bytes(r.data)
    print(f"\n✓ wrote {out}")
    return 0


def _print_analysis(an, prof) -> None:
    info = an.sliced.info
    print(f"{an.sliced.name}: {an.printer}, {', '.join(info.filaments) or '?'} on {info.bed_type or '?'}")
    print(f"profile: {prof.name}")
    for p in an.parts:
        push = an.plan.part_push.get(p.label)
        print(
            f"  part {p.name!r}: {p.height:.1f} mm tall at X{p.xmin:.0f}-{p.xmax:.0f} Y{p.ymin:.0f}-{p.ymax:.0f}"
            + (f", pushed down to Z{push:g}" if push is not None else ", NOT reached by the sweep")
        )
    for n in an.notes:
        print(f"  i {n}")
    for w in an.warnings:
        print(f"  ! {w}")
    for p in an.problems:
        print(f"  ✖ {p}")


def cmd_check(a) -> int:
    sf = threemf.load(a.input)
    findings = lint(sf)
    print(f"{sf.name}: {printers.model_name(sf.info.printer_model_id)}")
    for f in findings:
        print(f"  {ICON[f.level]} {f.title}\n      {f.detail}")
    return 1 if any(f.level == "danger" for f in findings) else 0


def cmd_import(a) -> int:
    try:
        r = import_profile(threemf.load(a.plain), threemf.load(a.farmloop), a.name)
        path = _profiles(a).save(r.profile)
    except (ImportError_, ValueError) as e:
        print(f"✖ {e}", file=sys.stderr)
        return 2
    print(f"✓ saved profile '{r.profile.name}' ({r.profile.id}) to {path}")
    for f in r.found:
        print(f"  found: {f}")
    for f in r.dropped:
        print(f"  left out: {f}")
    return 0


def cmd_profiles(a) -> int:
    for p in _profiles(a).all():
        kind = "built-in" if p.source.get("kind") == "builtin" else "imported"
        print(f"{p.id:28} {printers.model_name(p.printer_model_id):10} stage {p.stage}  {kind:9} {p.name}")
    return 0


def cmd_calibrate(a) -> int:
    sf = threemf.load(a.input)
    d = _defaults(a)
    prof = _pick_profile(a, sf, d)
    data, name = build_test(sf, prof, a.test, d.release_temp)
    out = Path(a.output) if a.output else Path(a.input).with_name(name)
    out.write_bytes(data)
    print(
        f"✓ wrote {out}\n  Print it and watch: it pauses between steps; press Resume on the printer to continue."
    )
    return 0


def cmd_app(a) -> int:
    import uvicorn

    from .server import create_app
    from .service import App

    app = App(a.data)
    url = f"http://{'localhost' if a.host in ('0.0.0.0', '127.0.0.1') else a.host}:{a.port}"
    print(f"ClearLoop {__version__} running at {url}  (data in {Path(a.data).resolve()})")
    if not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        uvicorn.run(create_app(app), host=a.host, port=a.port, log_level="warning")
    finally:
        app.shutdown()
    return 0


def _loop_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", dest="profile_id", help="hardware profile id (see `clearloop profiles`)")
    p.add_argument("--loops", type=int, help="how many times to print the plate")
    p.add_argument(
        "--release-temp", type=float, help="bed temperature to cool to before ejecting (default 30)"
    )
    p.add_argument(
        "--floor", dest="floor_z", type=float, help="lowest sweep height, from the sweep-floor test"
    )
    p.add_argument(
        "--natural-cooldown", dest="natural_cooldown_min", type=int, help="minutes, door shut, fans off"
    )
    p.add_argument("--cooldown-fan", choices=["profile", "exhaust", "off"])
    p.add_argument("--no-bender", action="store_true", help="the FarmBoard bender is not fitted")
    p.add_argument(
        "--wait-for-monitor",
        action="store_true",
        help="pause after each loop until the monitor checks the plate",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clearloop", description="Open-source loop files for FarmLoop hardware")
    ap.add_argument("--version", action="version", version=f"clearloop {__version__}")
    ap.add_argument(
        "--data", default=str(DEFAULT_DATA), help=f"settings, profiles and logs (default {DEFAULT_DATA})"
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("app", help="open the ClearLoop app in your browser")
    p.add_argument(
        "--host", default="127.0.0.1", help="use 0.0.0.0 to reach it from other devices on your network"
    )
    p.add_argument("--port", type=int, default=8484)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_app)

    p = sub.add_parser("make", help="turn a plain .gcode.3mf into a loop file")
    p.add_argument("input")
    p.add_argument("-o", "--output")
    p.add_argument("--plate", type=int)
    _loop_args(p)
    p.set_defaults(fn=cmd_make)

    p = sub.add_parser("check", help="check any loop file (FarmLoop or ClearLoop) for known problems")
    p.add_argument("input")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser(
        "import-profile", help="learn a hardware profile from a plain slice + its FarmLoop version"
    )
    p.add_argument("plain")
    p.add_argument("farmloop")
    p.add_argument("--name")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("profiles", help="list hardware profiles")
    p.set_defaults(fn=cmd_profiles)

    p = sub.add_parser("calibrate", help="make a guided test print")
    p.add_argument("test", choices=TESTS)
    p.add_argument("input", help="a small plate sliced for the printer")
    p.add_argument("-o", "--output")
    _loop_args(p)
    p.set_defaults(fn=cmd_calibrate)

    a = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.WARNING, format="%(levelname)s %(message)s"
    )
    try:
        return int(a.fn(a) or 0)
    except (threemf.PlateFileError, KeyError) as e:
        print(f"✖ {str(e).strip(chr(39))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
