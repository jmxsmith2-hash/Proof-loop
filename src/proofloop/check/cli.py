"""``proofloop check``: CAD designs and sliced G-code, before you print.

``proofloop check FILE`` picks the right check for the file; the commands below are the same checks
with every option, plus slicing (prepare), learning from your prints, and product predictions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from proofloop.check import summary
from proofloop.core.commands import ConfigOpt
from proofloop.core.commands import database as _db
from proofloop.core.commands import settings_or_exit as _settings
from proofloop.core.config import ConfigError, find_config_dir, load_settings
from proofloop.core.output import OK, PROBLEMS, emit, exit_code
from proofloop.core.paths import config_file

app = typer.Typer(help="Check CAD designs and sliced G-code before you print.", no_args_is_help=True,
                  add_completion=False)
SEVERITY_COLOR = {4: "red", 3: "bright_red", 2: "yellow", 1: "cyan"}


def _finding_line(f: dict, where: str = "") -> None:
    colour = SEVERITY_COLOR.get(f["severity"], "white")
    typer.echo(f"  {f['n']:2}. " + typer.style(f"{f['severity_name']:9}", fg=colour, bold=f["severity"] >= 3)
               + f"  {f['title']}{where}")


def _verdict(findings: list[dict], ready_text: str) -> None:
    blocking = sum(1 for f in findings if f["severity"] >= 3)
    if blocking:
        typer.secho(f"✖ {blocking} thing{'s' if blocking != 1 else ''} to fix first", fg="red", bold=True)
    else:
        minor = len(findings)
        typer.secho(f"✓ {ready_text}" + (f" ({minor} minor or note{'s' if minor != 1 else ''})" if minor else ""),
                    fg="green", bold=True)


@app.command("new")
def new_design(config: Path | None = ConfigOpt) -> None:
    """Answer a few questions to describe a design (how it is used, printer, material) for its checks."""
    import re

    import yaml

    from proofloop.core.config import load_yaml_file

    cfg_dir = Path(config) if config else find_config_dir()
    cad_file = Path(typer.prompt("Path to the CAD file (STL, 3MF, OBJ, STEP...)"))
    if not cad_file.is_file():
        typer.secho(f"{cad_file} does not exist yet; you can still finish and add it later.", fg="yellow")
    name = typer.prompt("Name of the design", default=cad_file.stem.replace("_", " "))
    what = typer.prompt("What is it, in a sentence?", default="")
    profiles = load_yaml_file(config_file(cfg_dir, "printers.yaml")) or {}
    printers, materials = sorted(profiles.get("printers", {})), sorted(profiles.get("materials", {}))
    printer = typer.prompt(f"Printer ({', '.join(printers)})", default=profiles.get("default_printer", ""))
    material = typer.prompt(f"Material ({', '.join(materials)})", default=profiles.get("default_material", "PLA"))
    handheld = typer.confirm("Is it held in the hand?", default=False)
    types = typer.prompt("Kind of product (everyday, electronics, wearable, 3d_printed; comma separated)",
                         default="3d_printed")
    design = {"name": name, "type": [t.strip() for t in types.split(",") if t.strip()], "description": what,
              "printer": printer, "material": material, "use": {"handheld": handheld}}
    out = Path("designs") / (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") + ".yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# Created by `proofloop check new`. See examples/cad/*.yaml for ports, buttons, fit and loads.\n"
                   + yaml.safe_dump(design, sort_keys=False, allow_unicode=True), encoding="utf-8")
    typer.secho(f"Saved {out}", fg="green")
    typer.echo(f"Next:  proofloop check design {cad_file} --design {out}")
    typer.echo(f"After printing:  proofloop check printed \"{name}\" --measure measurements.csv")


@app.command("review")
def product_review(
    product: Path = typer.Argument(..., help="Product YAML (spec, photos, CAD, reviews of similar products, sensors)."),
    count: int = typer.Option(5, "--personas", "-n", help="Number of personas to generate if none are given."),
    persona: list[str] = typer.Option([], "--persona", "-p", help="Use stored personas."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Output folder for the report."),
    config: Path | None = ConfigOpt,
) -> None:
    """Predict complaints for a physical product (prediction, not a test)."""
    from proofloop.check import review_product, write_report
    from proofloop.check.guardrails import LABEL
    from proofloop.check.inputs import load_product
    from proofloop.core.llm import LLMGateway
    from proofloop.personas.generate import PersonaGenerator, load_personas

    s = _settings(config)
    db = _db(s)
    spec = load_product(product)
    people = load_personas(db, ids=persona) if persona else asyncio.run(PersonaGenerator().generate(count))
    result = asyncio.run(review_product(db, spec, people, LLMGateway(s, db)))
    path = write_report(result, Path(out or s.data_dir / "reports" / result["product_id"]), people)
    typer.secho(f"{LABEL.upper()}.", fg="yellow")
    for row in result["summary"]:
        typer.echo(f"  {row['risk_label']:6} {row['category']:12} {row['title']}")
    typer.secho(f"report: {path}", fg="green")


@app.command("design")
def design_check(
    cad_file: Path = typer.Argument(..., help="STL, 3MF, OBJ, PLY, GLB (or STEP with proofloop[step])."),
    design: str | None = typer.Option(None, "--design", "-d", help="Design name, or a design YAML (use, load, ports...)."),
    printer: str | None = typer.Option(None, help="Printer profile from config/printers.yaml."),
    material: str | None = typer.Option(None, help="PLA, PETG, ABS, ASA, TPU, resin ..."),
    units: str | None = typer.Option(None, help="mm (default), cm, m or in, if the file was exported differently."),
    handheld: bool = typer.Option(False, "--handheld", help="It is held in the hand (grip and weight checks)."),
    auto_eject: bool = typer.Option(False, "--auto-eject", help="Printed in a FarmLoop loop: check the pusher "
                                                              "can clear it, and how many fit per plate."),
    process: str | None = typer.Option(None, "--process",
                                          help="How it will be made: print (default), injection or cnc."),
    pull: str | None = typer.Option(None, "--pull", help="Injection moulding: mould opening direction x, y, z "
                                                          "or auto (default: the one with the fewest undercuts)."),
    personas: int = typer.Option(5, "--personas", "-n", help="How many personas react to the design."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Report folder."),
    config: Path | None = ConfigOpt,
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON (for programs and assistants)."),
) -> None:
    """Check a CAD design before printing; run it again on each new version to see what changed."""
    from proofloop.check.cad.pipeline import check_design, design_spec
    from proofloop.personas.generate import PersonaGenerator

    if cad_file.name.lower().endswith((".gcode", ".gcode.3mf", ".gco")):
        if not as_json:
            typer.echo("This is sliced G-code; checking it as G-code (proofloop check gcode).")
        gcode_check(cad_file, printer=printer, material=material, plate=None, out=out, open_report=False,
                    config=config, as_json=as_json)
        return

    s = _settings(config)
    db = _db(s)
    spec = design_spec(design, cad_file)
    if handheld:
        spec.use["handheld"] = True
    if auto_eject:
        spec.use["auto_eject"] = True
    people = asyncio.run(PersonaGenerator(seed=11).generate(personas, include_accessibility=True))
    try:
        result = check_design(s, db, cad_file, spec, people, out, printer, material, units, process=process,
                              pull=pull)
    except ConfigError as exc:
        typer.secho(f"Cannot check {cad_file}: {exc}", fg="red", err=True)
        raise typer.Exit(2) from None
    code = exit_code(result["findings"])
    if as_json:
        emit(summary.design(result))
        raise typer.Exit(code)
    made = result["process"]
    pr = result["printer"]
    how = (pr.get("names") or [pr.get("label") or pr["name"]])[0] if made["kind"] == "print" else \
        made["label"] + (f", pull {made['pull'].upper()}" if made.get("pull") else "")
    typer.secho(f"{result['design']} v{result['version']}", bold=True, nl=False)
    typer.echo(f"  ({how}, {result['material']['name']})")
    _verdict(result["findings"], "Ready to print" if made["kind"] == "print" else "Ready to make")
    for f in result["findings"]:
        _finding_line(f)
    if not result["findings"]:
        typer.secho("  no problems found by the geometry checks", fg="green")
    est = result["metrics"]["estimates"]
    if made["kind"] == "print":
        support = f" + {est['support_grams']} g support" if est.get("support_grams") else ""
        typer.echo(f"  about {est['printed_grams']} g{support}, {est['print_hours']} h, material {est['material_cost']}")
    else:
        typer.echo("  " + "; ".join(f"{label}: {value}" for value, label in est.get("process_stats", [])))
    c = result["comparison"]
    if c:
        typer.echo(f"  since v{c['previous_version']}: {len(c['fixed'])} fixed, {len(c['persisting'])} still there, "
                   f"{len(c['new'])} new")
    from proofloop.check.cad.eject import copies_note

    note = copies_note(result["metrics"], est.get("printed_grams"))
    if note:
        typer.echo(f"  auto-eject: {note}")
    typer.secho(f"report: {result['report']}", fg="green")
    if made["kind"] == "print":
        typer.echo(f"After printing: proofloop check printed \"{result['design']}\" --measure measurements.csv "
                   "[--defect warping ...]")
    raise typer.Exit(code)


@app.command("prepare")
def prepare_print(
    model: Path = typer.Argument(..., help="STL, 3MF, OBJ ... to check, slice and check again."),
    printer: str | None = typer.Option(None, help="Printer profile (default from config/printers.yaml)."),
    material: str | None = typer.Option(None, help="PLA, PETG, ABS ... (default from config/printers.yaml)."),
    layer: float = typer.Option(0.2, help="Layer height in mm (picks the slicer's process preset)."),
    support: bool | None = typer.Option(None, "--support/--no-support", help="Turn support on or off (default: "
                                                                               "as the slicer's preset has it)."),
    slicer: Path | None = typer.Option(None, help="Path to Bambu Studio or OrcaSlicer (found automatically)."),
    process_preset: str | None = typer.Option(None, "--process-preset", help="The slicer's process preset name."),
    filament_preset: str | None = typer.Option(None, "--filament-preset", help="The slicer's filament preset name."),
    loops: int = typer.Option(0, help="Also make a loop file with this many loops, and check it."),
    auto_eject: bool = typer.Option(False, "--auto-eject", help="Check the FarmLoop pusher can clear it (on with --loops)."),
    fix: bool = typer.Option(False, "--fix", help="Apply the slicer settings that fix what was found, and slice again."),
    design: str | None = typer.Option(None, "--design", "-d", help="Design name or design YAML."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Report folder."),
    open_report: bool = typer.Option(False, "--open", help="Open the report in the browser."),
    config: Path | None = ConfigOpt,
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON (for programs and assistants)."),
) -> None:
    """From CAD to a print-ready verdict: design check, slice with Bambu Studio or Orca, G-code check, compare."""
    import webbrowser

    from proofloop.check.prepare import prepare

    s = _settings(config)
    history = _print_history(config, say=not as_json)
    try:
        r = prepare(s, _db(s), model, printer=printer, material=material, layer=layer, slicer=slicer,
                    process_preset=process_preset, filament_preset=filament_preset, support=support, loops=loops,
                    out=out, history=history, design=design, auto_eject=auto_eject or loops > 0, fix=fix)
    except ConfigError as exc:
        typer.secho(f"Cannot prepare {model}: {exc}", fg="red", err=True)
        raise typer.Exit(2) from None
    if as_json:
        emit(summary.prepare(r))
        raise typer.Exit(OK if r["ready"] else PROBLEMS)
    typer.secho("Ready to print" if r["ready"] else f"Fix {r['blocking']} thing(s) first",
                fg="green" if r["ready"] else "red", bold=True)
    typer.echo(f"  sliced by {r['slicer']['name']}: {r['presets']['process']}, {r['presets']['filament']}")
    typer.echo(f"  {r['grams']} g, {r['time']}, {r['layers']} layers")
    for rd in r["rounds"]:
        changed = ", ".join(f"{st['label']} = {st['value']}" for st in rd["applied"])
        typer.echo(f"  sliced again with {changed}: {rd['blocking_before']} -> {rd['blocking_after']} problem(s)")
    for f in r["slicer_findings"]:
        typer.echo(f"  [{ {4: 'will fail', 3: 'major', 2: 'minor', 1: 'note'}[f['severity']]:9}] {f['title']}")
    if r["setting_fixes"]:
        typer.echo("  slicer settings that would fix it (run with --fix to apply and slice again):")
        for st in r["setting_fixes"]:
            typer.echo(f"    {st['label']} = {st['value']}")
    for kind in ("design", "gcode"):
        for f in r[kind]["findings"]:
            if f["severity"] >= 3:
                typer.echo(f"  [{f['severity_name']:9}] {f['title']}  ({'design' if kind == 'design' else 'G-code'})")
    if r["loop"]:
        typer.echo(f"  loop file: {r['loop']['path']}")
    typer.secho(f"report: {r['report']}", fg="green")
    if open_report:
        webbrowser.open(Path(r["report"]).resolve().as_uri())
    raise typer.Exit(OK if r["ready"] else PROBLEMS)


@app.command("gcode")
def gcode_check(
    gcode_file: Path = typer.Argument(..., help="Sliced .gcode, or a Bambu Studio .gcode.3mf (Export plate sliced file)."),
    printer: str | None = typer.Option(None, help="Printer profile (default: the one the file was sliced for)."),
    material: str | None = typer.Option(None, help="PLA, PETG, ABS ... (default: the file's filament)."),
    plate: int | None = typer.Option(None, help="Plate number in a .gcode.3mf (default: the first)."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Report folder."),
    open_report: bool = typer.Option(False, "--open", help="Open the report in the browser."),
    config: Path | None = ConfigOpt,
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON (for programs and assistants)."),
) -> None:
    """Check sliced G-code before printing: plate, layers, temperatures, flow, retraction, cooling."""
    import webbrowser

    from proofloop.check.gcode.pipeline import check_gcode

    cfg = Path(config) if config else find_config_dir()
    history = _print_history(config, say=not as_json)
    try:
        r = check_gcode(cfg, gcode_file, printer=printer, material=material, plate=plate, out=out, history=history)
    except ConfigError as exc:
        typer.secho(f"Cannot check {gcode_file}: {exc}", fg="red", err=True)
        raise typer.Exit(2) from None
    code = exit_code(r["findings"])
    if as_json:
        emit(summary.gcode(r))
        raise typer.Exit(code)
    typer.secho(str(Path(r["file"]).name), bold=True, nl=False)
    typer.echo(f"  ({r['printer']['label']} {r['printer_source']}, {r['material']['name']} "
               f"{r['material_source']}, {r['nozzle']:g} mm nozzle)")
    _verdict(r["findings"], "Ready to print")
    for f in r["findings"]:
        _finding_line(f, typer.style(f"  layer {f['layer']}", dim=True) if f.get("layer") else "")
    if not r["findings"]:
        typer.secho("  no problems found in the G-code", fg="green")
    if r.get("setting_fixes"):
        typer.echo("  slicer settings to change:")
        for st in r["setting_fixes"]:
            typer.echo(f"    {st['label']} = {st['value']}")
    m = r["metrics"]
    when = r["time"]["slicer"] or f"about {r['time']['replay']}"
    lf = m.get("loop_file")
    if lf:
        pauses = f", waits for the plate check {lf['plate_check_pauses']}x" if lf["plate_check_pauses"] else ""
        typer.echo(f"  loop file by {lf['made_by']}: {lf['loops']} loops x {m.get('layers', 0)} layers, "
                   f"{m.get('filament_g_per_loop', 0)} g per loop ({m.get('filament_g', 0)} g in all), printing {when} "
                   f"plus cooldown and sweep{pauses}")
    else:
        typer.echo(f"  {m.get('layers', 0)} layers, {m.get('filament_g', 0)} g, {when}")
    typer.secho(f"report: {r['report']}", fg="green")
    if open_report:
        webbrowser.open(Path(r["report"]).resolve().as_uri())
    raise typer.Exit(code)


def _print_history(config: Path | None, files: list[Path] | None = None, quiet: bool = True, say: bool = True):
    """Import new prints from the print journal, then return a history lookup for the G-code check.

    ``say=False`` writes nothing on stdout (for --json)."""
    from proofloop.check.learn import history_for, import_prints

    try:
        s = load_settings(config)
    except ConfigError:
        return None
    db = _db(s)
    try:
        counts = import_prints(db, s.config_dir, None, files)
    except Exception as exc:  # noqa: BLE001 - learning must never stop a check
        if not quiet:
            typer.secho(f"Could not read the print journal: {exc}", fg="yellow", err=True)
        counts = {"new": 0}
    if counts["new"] and quiet and say:
        typer.echo(f"learned from {counts['new']} new print(s) on your printers")
    return lambda printer, material, job: history_for(db, printer, material, job)


@app.command("learn")
def learn_from_prints(
    files: list[Path] = typer.Option([], "--files", help="Folders with the sliced files you print (searched by name)."),
    config: Path | None = ConfigOpt,
) -> None:
    """Learn from how your prints ended (ProofLoop's printer monitor records them) and show what was learned."""
    from proofloop.check.learn import CAUSE_TEXT, history_for, import_prints

    s = _settings(config)
    db = _db(s)
    counts = import_prints(db, s.config_dir, None, files)
    typer.echo(f"{counts['new']} new print(s) ({counts['with_gcode']} with their G-code)")
    groups = db.all("SELECT printer, material, COUNT(*) AS n FROM print_outcomes WHERE printer IS NOT NULL "
                    "GROUP BY printer, material ORDER BY n DESC")
    if not groups:
        typer.echo("Nothing learned yet. Add your printer under Printers in the window; every print it sees is recorded.")
        return
    for g in groups:
        h = history_for(db, g["printer"], g["material"] or "?")
        typer.secho(f"\n{g['printer']} + {g['material']}: {h['prints']} prints, {h['failed']} failed", bold=True)
        for cause, n in sorted(h["by_cause"].items(), key=lambda kv: -kv[1]):
            typer.echo(f"  {n} failed {CAUSE_TEXT.get(cause, cause)}")
        if h["proven_flow"]:
            typer.echo(f"  finishes prints at up to {h['proven_flow']:.0f} mm³/s ({h['proven_prints']} prints)")
        if h["failed_flow"]:
            typer.echo(f"  has failed at about {h['failed_flow']:.0f} mm³/s")
        if h["eject_checked"]:
            typer.echo(f"  plate checks: {h['eject_left']} of {h['eject_checked']} found parts left")


@app.command("printed")
def record_printed(
    design: str = typer.Argument(..., help="Design name used with `proofloop check design`."),
    measure: Path | None = typer.Option(None, "--measure", "-m",
                                           help="CSV: feature, designed_mm, measured_mm, kind (outer|hole|gap), axis."),
    defect: list[str] = typer.Option([], "--defect", help="warping, stringing, elephant_foot, poor_overhang, "
                                     "bridge_sag, broke, too_tight, too_loose, layer_shift, poor_bed_adhesion, "
                                     "supports_hard_to_remove"),
    version: int | None = typer.Option(None, help="Which version was printed (default: latest)."),
    notes: str = typer.Option("", help="Anything else you noticed."),
    config: Path | None = ConfigOpt,
) -> None:
    """Record a real print. The next `proofloop check design` uses what it taught us about your printer."""
    from proofloop.check.cad.iterate import DEFECTS, read_measurements
    from proofloop.check.cad.pipeline import record_print

    db = _db(_settings(config))
    unknown = [d for d in defect if d not in DEFECTS]
    if unknown:
        typer.secho(f"Unknown defect(s) {unknown}; choose from {sorted(DEFECTS)}", fg="red", err=True)
        raise typer.Exit(2)
    rows = read_measurements(measure) if measure else []
    analysis = record_print(db, design, rows, list(defect), notes, version)
    typer.echo(f"recorded print of {design} v{analysis['version']} ({analysis['printer']}, {analysis['material']})")
    for d in analysis["deviations"]:
        typer.echo(f"  {d['feature']:20} designed {d['designed_mm']:g}  measured {d['measured_mm']:g}  "
                   f"({d['diff_mm']:+.2f} mm)")
    for a in analysis["advice"]:
        typer.echo(f"  - {a}")
    typer.secho(f"learned for this printer + material from {analysis['learned']['prints']} print(s); "
                "the next check applies it.", fg="green")


@app.command("history")
def design_history(design: str | None = typer.Argument(None, help="Design name (default: list all)."),
                     config: Path | None = ConfigOpt) -> None:
    """Versions of a design and how the findings changed."""
    db = _db(_settings(config))
    designs = db.all("SELECT * FROM designs" + (" WHERE name = ?" if design else "") + " ORDER BY created_at",
                     [design] if design else [])
    for d in designs:
        typer.echo(d["name"])
        for v in db.all("SELECT * FROM design_versions WHERE design_id = ? ORDER BY version", [d["id"]]):
            sev = [f["severity"] for f in v["findings_json"]]
            prints = db.one("SELECT COUNT(*) AS n FROM print_results WHERE version_id = ?", [v["id"]])["n"]
            comp = v["comparison_json"] or {}
            change = (f"  ({len(comp.get('fixed', []))} fixed, {len(comp.get('new', []))} new)" if comp else "")
            typer.echo(f"  v{v['version']}  {v['created_at'][:16]}  {len(sev)} findings, {sev.count(4)} will fail, "
                       f"{sev.count(3)} major{change}  prints recorded: {prints}  {v['report_path']}")
