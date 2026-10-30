"""``proofloop loop``: make, check and cost loop files, and make calibration prints. The window does all of this too."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from proofloop.core import printers
from proofloop.core.output import ERROR, OK, PROBLEMS, emit
from proofloop.core.paths import home
from proofloop.core.settings import Defaults, SettingsStore
from proofloop.loopfiles import threemf
from proofloop.loopfiles.api import pick_profile, swept_before
from proofloop.loopfiles.calibration import TESTS, build_test
from proofloop.loopfiles.gcode.processor import ProcessError, analyze, process
from proofloop.loopfiles.importer import ImportError_, import_profile
from proofloop.loopfiles.lint import lint
from proofloop.loopfiles.profiles import ProfileStore
from proofloop.monitor.filament import prices_for
from proofloop.monitor.journal import PrintJournal
from proofloop.monitor.service import App, loop_settings

ICON = {"danger": "✖", "warning": "!", "info": "i", "ok": "✓"}


def _profiles(a) -> ProfileStore:
    return ProfileStore(Path(a.data) / "profiles")


def _defaults(a) -> Defaults:
    d = SettingsStore(a.data).load().defaults.model_dump()
    for key in (
        "profile_id",
        "loops",
        "release_temp",
        "floor_z",
        "push_offset",
        "natural_cooldown_min",
        "cooldown_fan",
    ):
        v = getattr(a, key, None)
        if v is not None:
            d[key] = v
    if getattr(a, "no_bender", False):
        d["bender_fitted"] = False
    if getattr(a, "wait_for_monitor", False):
        d["wait_for_monitor"] = True
    if getattr(a, "first_pause", None):
        d["pause_first_sweep"] = a.first_pause
    return Defaults.model_validate(d)


def _part_push(a) -> dict[str, float]:
    out = {}
    for item in getattr(a, "part_push", None) or []:
        label, _, z = item.partition("=")
        try:
            out[label.strip()] = float(z)
        except ValueError:
            raise SystemExit(
                f"--part-push expects LABEL=HEIGHT, e.g. --part-push 104=1.5 (got {item!r})"
            ) from None
    return out


def _pick_profile(a, sf: threemf.SlicedFile, d: Defaults):
    store = _profiles(a)
    if getattr(a, "profile_id", None):
        return store.get(a.profile_id)
    profile = pick_profile(store, sf.info.printer_model_id, d.profile_id)
    if profile is None:
        raise SystemExit(
            f"no hardware profile for the {printers.model_name(sf.info.printer_model_id)} yet. Import one from a "
            "FarmLoop file: proofloop loop import-profile PLAIN.gcode.3mf FL_S1_PLAIN.gcode.3mf"
        )
    return profile


def cmd_make(a) -> int:
    sf = threemf.load(a.input, plate=a.plate)
    d = _defaults(a)
    prof = _pick_profile(a, sf, d)
    st = loop_settings(d, _part_push(a), swept_before(a.data))
    try:
        an = analyze(sf, prof, st)
        if not a.json:
            _print_analysis(an, prof)
        if not an.ok:
            if a.json:
                emit({"kind": "loop_file", "ready": False, "written": None, "profile": prof.id, **an.as_dict()})
            return PROBLEMS
        r = process(sf, prof, st)
    except ProcessError as e:
        print(f"✖ {e}", file=sys.stderr)
        return ERROR
    out = Path(a.output) if a.output else Path(a.input).with_name(r.filename)
    out.write_bytes(r.data)
    PrintJournal(None, a.data).keep_file(r.filename, r.data)  # so the print journal can find its G-code
    if a.json:
        emit({"kind": "loop_file", "ready": True, "written": str(out.resolve()), "loops": st.loops,
              "profile": prof.id, **an.as_dict()})
    else:
        print(f"\n✓ wrote {out}")
    return OK


def _print_analysis(an, prof) -> None:
    info = an.sliced.info
    print(f"{an.sliced.name}: {an.printer}, {', '.join(info.filaments) or '?'} on {info.bed_type or '?'}")
    print(f"profile: {prof.name}")
    for p in an.parts:
        push = an.plan.part_push.get(p.label)
        mine = " (your setting)" if p.label in an.plan.part_custom else ""
        print(
            f"  part {p.label} {p.name!r}: {p.height:.1f} mm tall at X{p.xmin:.0f}-{p.xmax:.0f} "
            f"Y{p.ymin:.0f}-{p.ymax:.0f}, push at Z{an.plan.part_target[p.label]:g}{mine}"
            + ("" if push is not None and push < p.height else ", NOT reached by the sweep")
        )
    for n in an.notes:
        print(f"  i {n}")
    for w in an.warnings:
        print(f"  ! {w}")
    for p in an.problems:
        print(f"  ✖ {p}")


def cmd_cost(a) -> int:
    from proofloop.monitor.costing import Times, cost

    sf = threemf.load(a.input)
    d = _defaults(a)
    prof = _pick_profile(a, sf, d)
    st = loop_settings(d)
    an = analyze(sf, prof, st)
    fixed = sum(x["seconds"] or 0 for x in an.steps if x["key"] != "print")
    cool = a.cooldown_min * 60 if a.cooldown_min is not None else None
    app = App(a.data, connect=False)
    r = cost(
        app.settings.costs,
        sf.info.filament_slots,
        sf.info.slot_grams,
        len(an.parts),
        Times(sf.info.print_seconds, fixed, cool, "your estimate" if cool is not None else "unknown"),
        a.loops or d.loops,
        prices_for(app.inventory, None, sf.info.filament_slots),
    )
    if a.json:
        emit({"kind": "cost", "file": sf.name, **r.as_dict()})
        return OK
    cur = r.currency
    print(f"{sf.name}: {r.parts_per_plate} parts per plate, {r.loops} loops")
    for ln in r.lines:
        val = f"{cur}{ln.per_loop:.3f} per loop" if ln.per_loop is not None else "not set"
        print(f"  {ln.label:18} {val:>17}   {ln.missing or ln.how}")
    if r.setup:
        print(f"  {'Setup (per run)':18} {cur}{r.setup:.3f}")
    t = r.totals
    print(
        f"\n  per part {cur}{t['per_part']:.3f} · per loop {cur}{t['per_loop']:.3f} · run {cur}{t['run']:.2f}"
    )
    if r.selling:
        s_ = r.selling
        print(
            f"  selling at {cur}{s_['price']:g}: profit {cur}{s_['profit']:.3f} per part ({s_['margin_pct']}% margin), "
            f"break-even {cur}{s_['breakeven_price']}, {s_['target_margin_pct']:g}% margin needs {cur}{s_['price_for_target']}"
        )
    for m in r.missing:
        print(f"  ! {m}")
    return 0


def cmd_check(a) -> int:
    from proofloop.loopfiles.print_checks import print_checks

    sf = threemf.load(a.input)
    findings = lint(sf)
    if a.json:
        found = [{**f.as_dict(), "from": "loop"} for f in findings]
        found += [{**f.as_dict(), "from": "print"} for f in print_checks(sf)]
        danger = any(f["level"] == "danger" for f in found)
        emit({"kind": "loop_check", "file": sf.name, "printer": printers.model_name(sf.info.printer_model_id),
              "ready": not danger, "findings": found})
        return PROBLEMS if danger else OK
    print(f"{sf.name}: {printers.model_name(sf.info.printer_model_id)}")
    for f in findings:
        print(f"  {ICON[f.level]} {f.title}\n      {f.detail}")
    extra = print_checks(sf)
    if extra:
        print("\n  Print checks:")
    for f in extra:
        print(f"  {ICON[f.level]} {f.title}\n      {f.detail}")
    findings += extra
    return PROBLEMS if any(f.level == "danger" for f in findings) else OK


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
    if a.json:
        emit([{"id": p.id, "name": p.name, "printer": printers.model_name(p.printer_model_id),
               "printer_model_id": p.printer_model_id, "stage": p.stage,
               "builtin": p.source.get("kind") == "builtin"} for p in _profiles(a).all()])
        return OK
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


def _loop_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", dest="profile_id", help="hardware profile id (see `proofloop loop profiles`)")
    p.add_argument("--loops", type=int, help="how many times to print the plate")
    p.add_argument(
        "--release-temp", type=float, help="bed temperature to cool to before ejecting (default 30)"
    )
    p.add_argument(
        "--floor", dest="floor_z", type=float, help="lowest sweep height, from the sweep-floor test"
    )
    p.add_argument(
        "--push-depth",
        dest="push_offset",
        type=float,
        help="push this far below each part's top (FarmLoop uses 10 mm)",
    )
    p.add_argument(
        "--part-push",
        action="append",
        metavar="LABEL=Z",
        help="your own push height for one part (labels are shown by `proofloop loop make`); repeatable",
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
    p.add_argument(
        "--first-pause",
        choices=["auto", "on", "off"],
        help="pause before the first sweep to check the pusher (auto: until a loop has swept cleanly)",
    )


COMMANDS = ("make", "check", "cost", "calibrate", "import-profile", "profiles")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        argv = ["make", *argv]  # proofloop loop PLATE = proofloop loop make PLATE
    ap = argparse.ArgumentParser(prog="proofloop loop", description="Loop files for FarmLoop auto-eject hardware. "
                                 "proofloop loop PLATE [options] makes one (the same as make).")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.set_defaults(data=str(home()), json=False)
    as_json = argparse.ArgumentParser(add_help=False)
    as_json.add_argument("--json", action="store_true", help="print the result as JSON (for programs and assistants)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("make", parents=[as_json], help="turn a plain .gcode.3mf into a loop file")
    p.add_argument("input")
    p.add_argument("-o", "--output")
    p.add_argument("--plate", type=int)
    _loop_args(p)
    p.set_defaults(fn=cmd_make)

    p = sub.add_parser("check", parents=[as_json], help="check any loop file (FarmLoop's or ProofLoop's) for known problems")
    p.add_argument("input")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("cost", parents=[as_json], help="what a plate costs and earns, from your prices (set them in the window)")
    p.add_argument("input")
    p.add_argument("--cooldown-min", type=float, help="your cooldown estimate, if ProofLoop hasn't measured one yet")
    _loop_args(p)
    p.set_defaults(fn=cmd_cost)

    p = sub.add_parser("calibrate", help="make a guided test print")
    p.add_argument("test", choices=TESTS)
    p.add_argument("input", help="a small plate sliced for the printer")
    p.add_argument("-o", "--output")
    _loop_args(p)
    p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("import-profile", help="learn a hardware profile from a plain slice + its FarmLoop version")
    p.add_argument("plain")
    p.add_argument("farmloop")
    p.add_argument("--name")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("profiles", parents=[as_json], help="list hardware profiles")
    p.set_defaults(fn=cmd_profiles)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return int(a.fn(a) or 0)
    except (threemf.PlateFileError, KeyError) as e:
        print(f"✖ {str(e).strip(chr(39))}", file=sys.stderr)
        return ERROR


if __name__ == "__main__":
    raise SystemExit(main())
