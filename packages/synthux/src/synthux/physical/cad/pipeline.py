"""`synthux physical check`: one CAD file -> design checks, persona reactions, version history, report."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from synthux import SYNTHETIC_DISCLAIMER
from synthux.core.config import Settings, load_yaml_file
from synthux.core.db import Database, new_id, utcnow
from synthux.core.logging import get_logger
from synthux.personas.models import Persona
from synthux.physical.cad.checks import run_checks
from synthux.physical.cad.ergonomics import collect_facts
from synthux.physical.cad.iterate import apply_learned, compare_versions, learned, metric_changes
from synthux.physical.cad.load import load_model, load_profiles
from synthux.physical.cad.personas import checklist, product_types, react
from synthux.physical.cad.render import best_view_for, render
from synthux.physical.guardrails import LABEL

log = get_logger(__name__)


@dataclass
class DesignSpec:
    name: str
    types: list[str] = field(default_factory=list)
    use: dict[str, Any] = field(default_factory=dict)
    printer: str | None = None
    material: str | None = None
    units: str | None = None
    description: str = ""
    features: list[dict[str, Any]] = field(default_factory=list)
    process: str | None = None        # None = 3D printing; injection | cnc
    pull: str | None = None           # injection moulding: x | y | z | auto


def design_spec(arg: str | None, cad_file: Path) -> DesignSpec:
    """`--design` may be a YAML file or just a name; default name = file name without _v2 etc."""
    if arg and arg.lower().endswith((".yaml", ".yml")):
        raw = load_yaml_file(Path(arg))
        types = raw.get("type", [])
        return DesignSpec(name=raw.get("name") or Path(arg).stem, types=[types] if isinstance(types, str) else types,
                          use=raw.get("use", {}), printer=raw.get("printer"), material=raw.get("material"),
                          units=raw.get("units"), description=raw.get("description", ""),
                          features=raw.get("features", []), process=raw.get("process"), pull=raw.get("pull"))
    stem = re.sub(r"[_\-\s]*v(?:er(?:sion)?)?\d+$", "", cad_file.stem, flags=re.I)
    return DesignSpec(name=arg or stem.replace("_", " "))


def _spread_hands(personas: list[Persona]) -> list[Persona]:
    """Make sure small hands, large hands and a weak grip are all represented (physical products)."""
    if not personas:
        return personas
    wanted = [("hand_size", "small"), ("hand_size", "large"), ("grip_strength", "low")]
    for attr, value in wanted:
        if not any(getattr(p.traits, attr) == value for p in personas):
            target = max(personas, key=lambda p: (getattr(p.traits, "hand_size") == "medium",
                                                  p.traits.grip_strength == "normal"))
            setattr(target.traits, attr, value)
    return personas


def _finding_dict(f: Any, idx: int) -> dict[str, Any]:
    return {"n": idx, "check": f.check, "severity": f.severity, "severity_name": f.severity_name, "title": f.title,
            "detail": f.detail, "fix": f.fix, "location": f.location, "data": f.data, "fingerprint": f.fingerprint}


def check_design(settings: Settings, db: Database, cad_file: Path, spec: DesignSpec, personas: list[Persona],
                 out_dir: Path | None = None, printer: str | None = None, material: str | None = None,
                 units: str | None = None, process: str | None = None, pull: str | None = None) -> dict[str, Any]:
    process = (process or spec.process or "").lower() or None
    model = load_model(cad_file, units or spec.units)
    if process and process not in ("print", "fdm", "resin"):
        from synthux.physical.cad.manufacture import load_process, run_process_checks

        printer_p, material_p = load_process(settings.config_dir, process, material or None)
        learned_data, material_eff = None, material_p
        ctx = run_process_checks(model, printer_p, material_p, spec.use, pull or spec.pull)
        process_info = {"kind": process, "label": printer_p["name"], "pull": ctx.metrics.get("pull")}
    else:
        printer_p, material_p, slicer = load_profiles(settings.config_dir, printer or spec.printer,
                                                      material or spec.material)
        learned_data = learned(db, printer_p["name"], material_p["name"])
        material_eff = apply_learned(material_p, learned_data)
        ctx = run_checks(model, printer_p, material_eff, slicer, spec.use)
        process_info = {"kind": "print", "label": "3D printing", "pull": None}
    facts = collect_facts(ctx)
    ctx.findings.sort(key=lambda f: -f.severity)
    findings = [_finding_dict(f, i + 1) for i, f in enumerate(ctx.findings)]

    types = product_types(spec.types, spec.use)
    personas = _spread_hands(personas)
    reactions = {p.id: [asdict(r) for r in react(p, facts, types)] for p in personas}
    reasons = {p.id: (reactions[p.id][0] if reactions[p.id] else None) for p in personas}  # forced choice

    design = db.one("SELECT * FROM designs WHERE name = ?", [spec.name])
    if design is None:
        design = {"id": new_id("dsn"), "name": spec.name}
        db.insert("designs", {"id": design["id"], "name": spec.name, "created_at": utcnow(),
                              "meta_json": {"types": types, "use": spec.use, "description": spec.description}})
    prev = db.one("SELECT * FROM design_versions WHERE design_id = ? ORDER BY version DESC LIMIT 1", [design["id"]])
    version = (prev["version"] + 1) if prev else 1
    comparison = None
    if prev and _kind(prev["printer"]) != process_info["kind"]:
        prev = None                           # a moulding or machining check is not compared with a print check
    if prev:
        comp = compare_versions(prev["findings_json"], findings, float(max(model.size)))
        comparison = {**comp, "previous_version": prev["version"], "same_file": prev["file_hash"] == model.file_hash,
                      "metrics": metric_changes(prev["metrics_json"], ctx.metrics)}
    prints = db.all("SELECT p.*, v.version FROM print_results p JOIN design_versions v ON v.id = p.version_id "
                    "WHERE v.design_id = ? ORDER BY p.id", [design["id"]])

    out_dir = Path(out_dir or settings.data_dir / "reports" / "designs" / f"{_slug(spec.name)}_v{version}")
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    markers = [{**f["location"], "label": str(f["n"]), "severity": f["severity"]} for f in findings if f["location"]]
    images = {}
    for view in ("iso", "front", "side", "below"):
        (assets / f"{view}.png").write_bytes(render(model.mesh, view, markers))
        images[view] = f"assets/{view}.png"
    for f in findings:
        if f["location"]:
            view = best_view_for(f["location"], model.mesh)
            mark = [{**f["location"], "label": str(f["n"]), "severity": f["severity"]}]
            (assets / f"finding_{f['n']}.png").write_bytes(render(model.mesh, view, mark, size=(420, 320)))
            f["image"] = f"assets/finding_{f['n']}.png"

    version_id = new_id("ver")
    result = {
        "design": spec.name, "design_id": design["id"], "version": version, "version_id": version_id,
        "file": str(cad_file), "file_hash": model.file_hash, "printer": printer_p, "material": material_eff,
        "learned": learned_data, "metrics": ctx.metrics, "findings": findings, "facts": _facts_json(facts),
        "types": types, "personas": [_persona_json(p) for p in personas], "reactions": reactions,
        "return_reasons": reasons, "checklist": checklist(types), "comparison": comparison,
        "prints": prints, "features": spec.features, "images": images, "label": LABEL,
        "disclaimer": SYNTHETIC_DISCLAIMER, "generated": utcnow(), "description": spec.description,
        "process": process_info,
    }
    from synthux.physical.cad.report import write_design_report

    path = write_design_report(result, out_dir)
    db.insert("design_versions", {
        "id": version_id, "design_id": design["id"], "version": version, "created_at": utcnow(), "file": str(cad_file),
        "file_hash": model.file_hash, "printer": printer_p["name"], "material": material_p["name"],
        "metrics_json": ctx.metrics, "findings_json": findings, "reactions_json": reactions,
        "comparison_json": comparison, "report_path": str(path)})
    result["report"] = str(path)
    log.info("%s v%d: %d findings (%d severe), report %s", spec.name, version, len(findings),
             sum(1 for f in findings if f["severity"] >= 3), path)
    return result


def _kind(stored_printer: str | None) -> str:
    """How an earlier version was checked, from the name stored with it."""
    from synthux.physical.cad.manufacture import PROCESSES

    return next((k for k, label in PROCESSES.items() if label == stored_printer), "print")


def _facts_json(facts: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in facts.items() if k != "findings"}


def _persona_json(p: Persona) -> dict[str, Any]:
    return {"id": p.id, "label": p.label, "traits": p.traits.model_dump(), "low_confidence": p.low_confidence,
            "note": p.confidence_note}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "design"


def record_print(db: Database, design_name: str, measurements: list[dict[str, Any]], defects: list[str],
                 notes: str = "", version: int | None = None) -> dict[str, Any]:
    """Store what came off the printer and learn from it for this printer + material."""
    from synthux.physical.cad.iterate import analyse_print, update_learned

    design = db.one("SELECT * FROM designs WHERE name = ?", [design_name])
    if design is None:
        raise KeyError(f"no design named {design_name!r}; run `synthux physical check` first")
    if version is None:
        row = db.one("SELECT * FROM design_versions WHERE design_id = ? ORDER BY version DESC LIMIT 1", [design["id"]])
    else:
        row = db.one("SELECT * FROM design_versions WHERE design_id = ? AND version = ?", [design["id"], version])
    if row is None:
        raise KeyError(f"{design_name!r} has no version {version}")
    analysis = analyse_print(measurements, defects)
    db.insert("print_results", {"version_id": row["id"], "created_at": utcnow(), "measurements_json": measurements,
                                "defects_json": defects, "notes": notes, "analysis_json": analysis})
    analysis["learned"] = update_learned(db, row["printer"], row["material"], analysis)
    analysis["version"] = row["version"]
    analysis["printer"], analysis["material"] = row["printer"], row["material"]
    return analysis
