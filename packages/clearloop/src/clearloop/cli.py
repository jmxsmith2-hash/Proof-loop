"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__, manifest, protocol, threemf
from .config import Config, load_config
from .cost import cost_per_part
from .gcode.processor import ProcessOptions, UnsupportedMaterial, process
from .gcode.routines import LoopSettings
from .gcode.sweep import SweepSettings
from .profiles import known_models, printer_profile


def _options_from_args(a) -> ProcessOptions:
    if a.config and a.printer_name:
        from .farm import process_options

        cfg = load_config(a.config)
        opts = process_options(cfg, cfg.printer(a.printer_name), copies=a.copies)
        if a.allow_unsupported:
            opts.allow_unsupported = True
        return opts
    if not a.printer:
        raise SystemExit("give --printer MODEL, or --config FILE with --printer-name NAME")
    custom = Path(a.custom_gcode).read_text() if a.custom_gcode else ""
    loop = LoopSettings(
        eject_mode=a.eject,
        release_temp=a.release_temp,
        natural_cooldown_min=a.natural_min,
        door_opener=a.door,
        bend_window_s=a.bend_window,
        cooldown_mode=a.cooldown,
        cooldown_time_s=a.cooldown_time,
        custom_eject_gcode=custom,
        sweep=SweepSettings(final_wipe=a.final_wipe),
    )
    return ProcessOptions(
        printer=printer_profile(a.printer),
        loop=loop,
        copies=a.copies,
        bed_temp=a.bed_temp,
        secret=a.secret,
        allow_unsupported=a.allow_unsupported,
    )


def _add_process_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("input", help="sliced .gcode.3mf or .gcode")
    p.add_argument("--printer", help=f"printer model ({', '.join(known_models())})")
    p.add_argument("--config", help="farm config file (use with --printer-name)")
    p.add_argument("--printer-name", help="printer name from the config file")
    p.add_argument("--plate", type=int, help="plate number inside a multi-plate 3MF")
    p.add_argument(
        "--eject",
        choices=["board", "gcode", "custom"],
        default="gcode",
        help="board: external bender (ClearBoard etc.); gcode: sweep only; custom: your snippet",
    )
    p.add_argument("--custom-gcode", help="file with your hardware's eject snippet (--eject custom)")
    p.add_argument("--copies", type=int, default=1, help="repeat the plate N times in one file")
    p.add_argument("--bed-temp", type=float, help="override the bed temperature (written exactly)")
    p.add_argument("--release-temp", type=float, help="override the release temperature")
    p.add_argument("--natural-min", type=int, help="override warp-safe natural cooldown minutes")
    p.add_argument("--cooldown", choices=["temperature", "time"], default="temperature")
    p.add_argument("--cooldown-time", type=int, default=600, help="seconds, with --cooldown time")
    p.add_argument("--door", action="store_true", help="a door opener is fitted")
    p.add_argument("--bend-window", type=int, default=25, help="seconds the printer waits for the bender")
    p.add_argument("--final-wipe", action="store_true", help="add low full-plate wipe passes")
    p.add_argument("--secret", help="sign the manifest with this farm secret")
    p.add_argument("--allow-unsupported", action="store_true", help="process TPU/PVA anyway")


def cmd_process(a) -> int:
    opts = _options_from_args(a)
    sliced = threemf.load(a.input, plate=a.plate)
    try:
        r = process(sliced, opts)
    except UnsupportedMaterial as e:
        print(f"error: {e} (use --allow-unsupported to override)", file=sys.stderr)
        return 2
    out = Path(a.output) if a.output else Path(a.input).with_name("clearloop_" + Path(a.input).name)
    out.write_bytes(r.data)
    print(f"wrote {out}")
    print(
        f"  material {r.material.name}, {len(r.objects)} objects, {len(r.plan.passes)} sweep passes, "
        f"~{r.cycle_seconds / 60:.0f} min per cycle"
    )
    for w in r.warnings:
        print(f"  warning: {w}")
    return 0


def cmd_inspect(a) -> int:
    opts = _options_from_args(a)
    sliced = threemf.load(a.input, plate=a.plate)
    try:
        r = process(sliced, opts)
    except UnsupportedMaterial as e:
        print(f"unsupported: {e}")
        return 2
    info = sliced.info
    costs = load_config(a.config).costs if a.config else Config().costs
    report = {
        "plate": sliced.plate,
        "container": sliced.container,
        "printer_profile": opts.printer.model,
        "sliced_for": info.printer_model,
        "filaments": info.filaments,
        "material_profile": r.material.__dict__,
        "print_seconds": info.print_seconds,
        "cycle_seconds_est": round(r.cycle_seconds),
        "objects": [o.as_dict() for o in r.objects],
        "sweep": r.plan.as_dict(),
        "warnings": r.warnings,
        "cost": cost_per_part(
            info.filament_grams, info.print_seconds, r.cycle_seconds, len(r.objects), costs
        ),
    }
    if a.json:
        print(json.dumps(report, indent=2))
        return 0
    print(
        f"plate {sliced.plate} ({sliced.container}) for {opts.printer.model}, sliced for {info.printer_model or '?'}"
    )
    print(
        f"filaments: {', '.join(info.filaments) or 'unknown'} -> release {r.material.release_temp} C, "
        f"natural cooldown {r.manifest['natural_cooldown_min']} min"
    )
    print(f"print {info.print_seconds / 60:.0f} min, full cycle ~{r.cycle_seconds / 60:.0f} min")
    print(f"cost per part: {report['cost']['per_part']} {report['cost']['currency']}")
    print("\ntop view (back of the plate at the top):")
    print(r.heightmap.ascii())
    print("\nobjects:")
    for i, o in enumerate(r.objects):
        print(f"  {i}: x {o.xmin:.0f}-{o.xmax:.0f}  y {o.ymin:.0f}-{o.ymax:.0f}  h {o.height:.1f} mm")
    print(f"\nsweep plan ({r.plan.est_seconds:.0f} s):")
    for i, p in enumerate(r.plan.passes):
        print(
            f"  {i + 1}: x {p.x:.1f}, from y {p.y_start:.0f}, push at z {p.push_z:.1f}, travel z {p.travel_z:.1f}"
        )
    for w in r.warnings:
        print(f"warning: {w}")
    return 0


def cmd_verify_manifest(a) -> int:
    sliced = threemf.load(a.input, plate=a.plate)
    v = manifest.verify(sliced.gcode, a.secret)
    print(
        json.dumps(
            {
                "present": v.present,
                "gcode_matches": v.gcode_matches,
                "signature": v.signature,
                "manifest": v.data,
            },
            indent=2,
        )
    )
    return 0 if v.ok else 1


def cmd_models(a) -> int:
    for m in known_models():
        p = printer_profile(m)
        print(
            f"{m:8} {p.bed_x:g}x{p.bed_y:g}x{p.max_z:g}  enclosed={p.enclosed!s:5}  camera={p.camera:8} {p.notes}"
        )
    return 0


def _farm(a, connect=True):
    from .farm import Farm

    cfg = load_config(a.config)
    farm = Farm(cfg, connect=connect)
    farm.config_dir = Path(a.config).resolve().parent
    return farm


def _connected_link(a):
    from .printer.bambu import BambuPrinter

    cfg = load_config(a.config)
    p = cfg.printer(a.name)
    link = BambuPrinter(p.host, p.serial, p.access_code, p.camera_kind(), p.snapshot_url)
    return cfg, p, link


def cmd_serve(a) -> int:
    import uvicorn

    from .server import create_app

    farm = _farm(a)
    farm.start_background()
    try:
        uvicorn.run(create_app(farm), host=a.host, port=a.port, log_level="info")
    finally:
        farm.stop()
    return 0


def cmd_snapshot(a) -> int:
    _, _, link = _connected_link(a)
    Path(a.output).write_bytes(link.snapshot())
    print(f"wrote {a.output}")
    return 0


def cmd_reference(a) -> int:
    from .farm import make_verifier

    cfg, p, link = _connected_link(a)
    verifier = make_verifier(cfg, p)
    if verifier is None:
        print("verification is disabled for this printer", file=sys.stderr)
        return 1
    if a.reset:
        verifier.clear_references()
    jpeg = Path(a.image).read_bytes() if a.image else link.snapshot()
    path = verifier.add_reference(jpeg)
    print(f"saved {path} ({len(verifier.references)} reference(s))")
    return 0


def cmd_check_bed(a) -> int:
    from .farm import make_verifier

    cfg, p, link = _connected_link(a)
    verifier = make_verifier(cfg, p)
    if verifier is None:
        print("verification is disabled for this printer", file=sys.stderr)
        return 1
    jpeg = Path(a.image).read_bytes() if a.image else link.snapshot()
    res = verifier.check(jpeg)
    if a.diff and res.diff_png:
        Path(a.diff).write_bytes(res.diff_png)
    print(json.dumps(res.as_dict(), indent=2))
    return 0 if res.clear else 1


def cmd_probe_marker(a) -> int:
    """Prove that ``M73 L`` markers come back over MQTT on this printer and firmware."""
    cfg, p, link = _connected_link(a)
    base = cfg.loop_for(p).marker_base
    link.connect()
    deadline = time.time() + 20
    while not link.state.online and time.time() < deadline:
        time.sleep(0.2)
    if not link.state.online:
        print("could not connect over MQTT; check LAN-only mode, Developer Mode and the access code")
        return 2
    if link.state.gcode_state in ("RUNNING", "PREPARE", "PAUSE"):
        print("printer is busy; run this while it is idle")
        return 2
    want = base + int(protocol.Marker.SWEEP_DONE)
    link.gcode_line(f"M73 L{want}")
    deadline = time.time() + 20
    while time.time() < deadline and link.state.layer_num != want:
        time.sleep(0.2)
    got = link.state.layer_num
    link.gcode_line("M73 L0")
    link.close()
    print(f"firmware {link.state.firmware or '?'}: sent M73 L{want}, printer reported layer_num={got}")
    if got == want:
        print("OK: the marker channel works on this printer")
        return 0
    print(
        "FAILED: markers are not echoed. Do not run board-triggered loops on this firmware; "
        "use the limit-switch backup or eject mode 'gcode' until this passes."
    )
    return 1


def cmd_enqueue(a) -> int:
    farm = _farm(a, connect=False)
    ids = farm.enqueue_file(
        a.input,
        copies=a.copies,
        printer=a.on,
        priority=a.priority,
        models=[m.strip().lower() for m in (a.models or "").split(",") if m.strip()],
    )
    print(f"queued jobs {ids}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clearloop", description="Open-source closed-loop continuous printing")
    ap.add_argument("--version", action="version", version=f"clearloop {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("process", help="make a sliced file loop-ready")
    _add_process_args(p)
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_process)

    p = sub.add_parser(
        "inspect", help="show materials, part layout, sweep plan and cost without writing a file"
    )
    _add_process_args(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("verify-manifest", help="check a processed file's manifest and signature")
    p.add_argument("input")
    p.add_argument("--plate", type=int)
    p.add_argument("--secret")
    p.set_defaults(fn=cmd_verify_manifest)

    p = sub.add_parser("models", help="list built-in printer profiles")
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("serve", help="run the farm controller, API and dashboard")
    p.add_argument("--config", required=True)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8484)
    p.set_defaults(fn=cmd_serve)

    for name, fn, helptext in (
        ("snapshot", cmd_snapshot, "save one camera frame"),
        ("reference", cmd_reference, "capture an empty-plate reference image"),
        ("check-bed", cmd_check_bed, "check whether the plate is empty"),
        ("probe-marker", cmd_probe_marker, "prove the MQTT marker channel works on this firmware"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--config", required=True)
        p.add_argument("name", help="printer name from the config")
        if name == "snapshot":
            p.add_argument("-o", "--output", default="snapshot.jpg")
        if name in ("reference", "check-bed"):
            p.add_argument("--image", help="use this JPEG instead of the live camera")
        if name == "reference":
            p.add_argument("--reset", action="store_true", help="delete existing references first")
        if name == "check-bed":
            p.add_argument("--diff", help="write the difference image here")
        p.set_defaults(fn=fn)

    p = sub.add_parser("enqueue", help="add plates to the queue")
    p.add_argument("--config", required=True)
    p.add_argument("input")
    p.add_argument("--copies", type=int, default=1)
    p.add_argument("--on", help="pin to one printer")
    p.add_argument("--models", help="comma-separated compatible models")
    p.add_argument("--priority", type=int, default=0)
    p.set_defaults(fn=cmd_enqueue)

    a = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
