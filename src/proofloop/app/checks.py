"""The Check tab: drop a CAD file or sliced G-code, and the check runs in the background.

Jobs show their progress on the page; each finished job links to its report. The page and its
API are served by the window's own server under ``/check`` (see ``register``), so only the window,
on this computer, can use them: the window refuses other host names, and every request that changes
something must carry the window's header and come from its own origin.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from proofloop.check.feedback import path as feedback_path
from proofloop.core import theme
from proofloop.core.config import ConfigError, Settings, load_yaml_file
from proofloop.core.db import Database
from proofloop.core.logging import get_logger
from proofloop.core.paths import config_file

log = get_logger(__name__)

MAX_UPLOAD = 1024 * 1024 * 1024          # 1 GB: large Bambu project files
CAD_SUFFIXES = (".stl", ".3mf", ".obj", ".ply", ".glb", ".gltf", ".off", ".step", ".stp", ".scad")
GCODE_SUFFIXES = (".gcode", ".gcode.3mf", ".gco", ".bgcode")
PREFIX = "/check"


NOTIFY_AFTER_S = 30  # only jobs that take this long are worth a phone alert


@dataclass
class Job:
    id: str
    kind: str                      # cad | gcode
    title: str
    status: str = "queued"         # queued | running | done | failed
    stage: str = "waiting"
    progress: float | None = None  # 0..1 when known
    created: float = field(default_factory=time.time)
    finished: float | None = None
    report: str | None = None      # path of index.html
    summary: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    source: str | None = None      # the uploaded plate, when it can be made into a loop file
    download: str | None = None    # a file made for you (a loop file)
    findings: list[dict[str, Any]] = field(default_factory=list)  # the top ones, to mark "not a problem"
    printer: str | None = None     # what it was checked for, so your verdicts apply to the same setup
    material: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("report")
        data.pop("source")
        data["report_url"] = f"{PREFIX}/job/{self.id}/" if self.report else None
        data["can_loop"] = bool(self.source)
        data["download"] = f"{PREFIX}/job/{self.id}/download/{Path(self.download).name}" if self.download else None
        return data


def safe_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .") or "upload"
    return base[:120]


def file_kind(name: str) -> str | None:
    low = name.lower()
    if low.endswith(GCODE_SUFFIXES):
        return "gcode"
    if low.endswith(CAD_SUFFIXES):
        return "cad"
    return None


class Checks:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.root = (settings.data_dir / "checks").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.feedback_file = feedback_path()
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.checks = ThreadPoolExecutor(max_workers=2, thread_name_prefix="proofloop-check")
        self.env = theme.environment("proofloop.app", "templates")
        self._load_jobs()

    # ------------------------------------------------------------------ saved jobs
    def _save(self, job: Job) -> None:
        """Each job is kept in its folder, so the list survives closing the app."""
        folder = self.root / job.id
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "job.json.tmp"
        tmp.write_text(json.dumps(asdict(job)), encoding="utf-8")
        tmp.replace(folder / "job.json")

    def _load_jobs(self, keep: int = 60) -> None:
        saved = []
        for f in self.root.glob("*/job.json"):
            try:
                saved.append(Job.from_dict(json.loads(f.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        for job in sorted(saved, key=lambda j: -j.created)[:keep]:
            if job.status in ("queued", "running"):
                job.status, job.error = "failed", "ProofLoop was closed before this finished; run it again"
            self.jobs[job.id] = job

    def remove(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise FileNotFoundError(job_id)
            if job.status in ("queued", "running"):
                raise ValueError("wait until it has finished")
            self.jobs.pop(job_id)
        shutil.rmtree(self.root / job_id, ignore_errors=True)

    def close(self) -> None:
        self.checks.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ your verdicts
    def feedback(self, job_id: str, check: str, verdict: str) -> dict[str, Any]:
        from proofloop.check import feedback

        with self.lock:
            job = self.jobs.get(job_id)
        f = next((f for f in (job.findings if job else []) if f["check"] == check), None)
        if job is None or f is None or not job.printer:
            raise ValueError("that finding isn't in this job")
        counts = feedback.record(self.feedback_file, check, job.printer, job.material or "", verdict, f["title"],
                                 job.title)
        f["verdict"] = verdict
        self._save(job)
        return counts

    # ------------------------------------------------------------------ page
    def page(self) -> str:
        from proofloop.check.my_printers import farm_printers

        data = load_yaml_file(config_file(self.settings.config_dir, "printers.yaml")) or {}
        printers = [{"id": k, "label": (v.get("names") or [k])[0], "type": v.get("type", "fdm")}
                    for k, v in (data.get("printers") or {}).items()]
        farm = farm_printers(data)  # the printers you added under Printers: one list for everything
        materials = list((data.get("materials") or {}).keys())
        procs = load_yaml_file(config_file(self.settings.config_dir, "processes.yaml")) or {}
        process_materials = {"print": materials, **{k: list((v or {}).get("materials", {})) for k, v in procs.items()}}
        try:
            from proofloop.check.slicer import find_slicer

            found = find_slicer()
            slicer = found.label if found and found.profile_dirs else None
        except Exception:  # noqa: BLE001 - a broken slicer install just hides the option
            slicer = None
        return self.env.get_template("check_app.html.j2").render(
            printers=printers, materials=materials, default_printer=data.get("default_printer"),
            process_materials=process_materials, slicer=slicer, default_material=data.get("default_material"),
            prefix=PREFIX, farm=farm, default_farm=farm[0]["id"] if farm else None)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.lock:
            return [j.public() for j in sorted(self.jobs.values(), key=lambda j: -j.created)]

    # ------------------------------------------------------------------ jobs
    def _new(self, kind: str, title: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, title=title)
        with self.lock:
            self.jobs[job.id] = job
        self._save(job)
        return job

    def _run(self, job: Job, work: Callable[[Job], None]) -> None:
        job.status, job.stage = "running", "starting"
        self._save(job)
        try:
            work(job)
            job.status, job.stage = "done", "finished"
        except (ConfigError, ValueError, PermissionError) as exc:
            job.status, job.error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 - report any failure in the window, keep the server up
            log.exception("check %s failed", job.id)
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            job.finished = time.time()
            try:
                self._save(job)
            except OSError:
                log.exception("could not save job %s", job.id)
            if job.finished - job.created >= NOTIFY_AFTER_S:
                self._notify(job)

    @staticmethod
    def _notify(job: Job) -> None:
        from proofloop.core import notify

        if job.status == "done":
            worst = "; ".join(job.summary[:2]) or "finished"
            notify.send(f"ProofLoop: {job.title} is done", worst)
        else:
            notify.send(f"ProofLoop: {job.title} failed", job.error or "see the window")

    def submit_file(self, filename: str, body_reader: Callable[[Path], int], options: dict[str, str]) -> Job:
        name = safe_name(filename)
        kind = options.get("kind") or file_kind(name)
        if kind not in ("cad", "gcode"):
            raise ValueError(f"{name}: drop a CAD file ({', '.join(CAD_SUFFIXES)}) or sliced G-code "
                             f"({', '.join(GCODE_SUFFIXES)})")
        job = self._new(kind, name)
        folder = self.root / job.id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        try:
            body_reader(path)
        except Exception:
            with self.lock:
                self.jobs.pop(job.id, None)
            shutil.rmtree(folder, ignore_errors=True)
            raise
        work = self._cad_work if kind == "cad" else self._gcode_work
        self.checks.submit(self._run, job, lambda j: work(j, path, options))
        return job

    def _cad_work(self, job: Job, path: Path, opt: dict[str, str]) -> None:
        from proofloop.check.cad.pipeline import check_design, design_spec
        from proofloop.personas.generate import PersonaGenerator

        job.stage = "measuring the geometry"
        spec = design_spec(opt.get("design") or None, path)
        if opt.get("handheld") in ("1", "true", "on"):
            spec.use["handheld"] = True
        if opt.get("auto_eject") in ("1", "true", "on"):
            spec.use["auto_eject"] = True
        people = asyncio.run(PersonaGenerator(seed=11).generate(5, include_accessibility=True))
        process = opt.get("process") if opt.get("process") in ("injection", "cnc") else None
        if not process and opt.get("slice") in ("1", "true", "on"):
            return self._prepare_work(job, path, opt)
        if process:
            job.stage = {"injection": "checking it for injection moulding", "cnc": "checking it for CNC machining"}[process]
        result = check_design(self.settings, self.db, path, spec, people, path.parent / "report",
                              opt.get("printer") or None, opt.get("material") or None, opt.get("units") or None,
                              process=process, pull=opt.get("pull") or None)
        job.report = result["report"]
        job.title = f"{result['design']} v{result['version']}"
        self._summarise(job, result["findings"], result["printer"]["name"], result["material"]["name"])
        est = result["metrics"]["estimates"]
        if result["process"]["kind"] == "print":
            printer = (result["printer"].get("names") or [result["printer"]["name"]])[0]
            job.summary.append(f"about {est['printed_grams']} g, {est['print_hours']} h on {printer}")
        else:
            job.summary.append(f"{result['process']['label']} in {result['material']['name']}: " +
                               "; ".join(f"{label} {value}" for value, label in est.get("process_stats", [])[:3]))

    def _prepare_work(self, job: Job, path: Path, opt: dict[str, str]) -> None:
        from proofloop.check.learn import history_for
        from proofloop.check.prepare import prepare

        job.stage = "checking the design, slicing it, then checking the G-code"
        support = {"on": True, "off": False}.get(opt.get("support") or "")
        r = prepare(self.settings, self.db, path, printer=opt.get("printer") or None,
                    material=opt.get("material") or None, support=support, out=path.parent / "report",
                    loops=int(opt["loops"]) if (opt.get("loops") or "").isdigit() else 0, design=opt.get("design") or None,
                    auto_eject=opt.get("auto_eject") in ("1", "true", "on"),
                    history=lambda p, m, j: history_for(self.db, p, m, j))
        job.report = r["report"]
        job.title = f"{r['model']}: {'ready to print' if r['ready'] else 'fix first'}"
        everything = r["slicer_findings"] + r["design"]["findings"] + r["gcode"]["findings"]
        self._summarise(job, sorted(everything, key=lambda f: -f["severity"]), r["printer"]["name"],
                        r["material"]["name"])
        job.counts = {"fail": sum(f["severity"] == 4 for f in everything),
                      "major": sum(f["severity"] == 3 for f in everything), "total": len(everything)}
        job.summary = ([f"Ready to print: {r['grams']} g, {r['time']}" if r["ready"] else
                        f"Fix {r['blocking']} thing(s) first ({r['grams']} g, {r['time']})"]
                       + [f["title"] for f in sorted(everything, key=lambda f: -f["severity"])[:4] if f["severity"] >= 3])

    def _gcode_work(self, job: Job, path: Path, opt: dict[str, str]) -> None:
        from proofloop.check.gcode.pipeline import check_gcode
        from proofloop.check.learn import history_for, import_prints

        job.stage = "reading what your printers recorded about earlier prints"
        try:
            import_prints(self.db, self.settings.config_dir)
        except Exception as exc:  # noqa: BLE001 - learning must never stop a check
            log.warning("could not read the print journal: %s", exc)
        job.stage = "reading every move"
        result = check_gcode(self.settings.config_dir, path, printer=opt.get("printer") or None,
                             history=lambda p, m, j: history_for(self.db, p, m, j),
                             material=opt.get("material") or None,
                             plate=int(opt["plate"]) if (opt.get("plate") or "").isdigit() else None,
                             out=path.parent / "report")
        job.report = result["report"]
        self._summarise(job, result["findings"], result["printer"]["name"], result["material"]["name"])
        when = result["time"]["slicer"] or result["time"]["replay"]
        m = result["metrics"]
        if m.get("loop_file"):
            lf = m["loop_file"]
            job.summary.append(f"loop file by {lf['made_by']}: {lf['loops']} loops, {m.get('filament_g_per_loop', 0)} g "
                               f"per loop, printing {when}")
        else:
            job.summary.append(f"{result['printer']['label']}, {result['material']['name']}: "
                               f"{m.get('filament_g', 0)} g, {when}")
            if path.name.lower().endswith(".3mf"):
                job.source = str(path)

    def submit_loop(self, job_id: str, loops: int) -> Job:
        """Turn a checked Bambu plate into a loop file, then check the loop file."""
        src = self.jobs.get(job_id)
        if src is None or not src.source:
            raise ValueError("that job has no Bambu plate to make a loop file from")
        if not 1 <= loops <= 500:
            raise ValueError("loops must be between 1 and 500")
        job = self._new("gcode", f"{src.title} \u00d7 {loops} loops")
        folder = self.root / job.id
        folder.mkdir(parents=True, exist_ok=True)
        self.checks.submit(self._run, job, lambda j: self._loop_work(j, Path(src.source), loops, folder))
        return job

    def _loop_work(self, job: Job, plate: Path, loops: int, folder: Path) -> None:
        from proofloop.check.prepare import make_loop_file

        job.stage = "making the loop file"
        made, note = make_loop_file(plate, loops, folder)
        job.download = str(made)
        self._gcode_work(job, made, {})
        job.title = made.name
        job.summary.insert(0, f"made with {note}")

    @staticmethod
    def _summarise(job: Job, findings: list[dict[str, Any]], printer: str | None = None,
                   material: str | None = None) -> None:
        job.printer, job.material = printer, material
        job.findings = [{"check": f["check"], "title": f["title"], "severity": f["severity"],
                         "severity_name": f["severity_name"]} for f in findings[:6]]
        job.counts = {"fail": sum(f["severity"] == 4 for f in findings), "major": sum(f["severity"] == 3 for f in findings),
                      "total": len(findings)}
        job.summary = [f"{f['severity_name']}: {f['title']}" for f in findings[:4]]
        if not findings:
            job.summary = ["no problems found"]

    # ------------------------------------------------------------------ files
    def job_source(self, job_id: str) -> tuple[bytes, str]:
        """The plate a finished check was run on, to open in Make a loop file."""
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None or not job.source or job.status != "done":
            raise ValueError("that job has no checked Bambu plate to make a loop file from")
        path = Path(job.source)
        return path.read_bytes(), path.name

    def submit_bytes(self, filename: str, data: bytes, options: dict[str, str] | None = None) -> Job:
        """Check a file another part of ProofLoop already has (a plate handed over from Loop files)."""
        def write(dest: Path) -> int:
            if not data:
                raise ValueError("the file is empty")
            dest.write_bytes(data)
            return len(data)

        return self.submit_file(filename, write, dict(options or {}))

    def job_file(self, job_id: str, rel: str) -> Path:
        job = self.jobs.get(job_id)
        if job is not None and job.download and rel == f"download/{Path(job.download).name}":
            return Path(job.download)
        if job is None or not job.report:
            raise FileNotFoundError(job_id)
        base = Path(job.report).resolve().parent
        target = (base / (rel or "index.html")).resolve()
        if target != base and base not in target.parents:
            raise PermissionError(rel)
        return target / "index.html" if target.is_dir() else target


def register(api: FastAPI, checks: Checks, window_only: Callable[[Request], None]) -> None:
    """The Check page and its API on the window's server. Everything that changes something is
    ``window_only`` (the window's header and origin)."""

    def fail(exc: Exception) -> JSONResponse:
        status = 404 if isinstance(exc, FileNotFoundError) else 400
        return JSONResponse({"error": "no such job" if status == 404 else str(exc)}, status)

    async def body_json(request: Request) -> dict[str, Any]:
        window_only(request)
        try:
            data = json.loads(await request.body() or b"{}")
        except ValueError as e:
            raise HTTPException(400, "send JSON") from e
        if not isinstance(data, dict):
            raise HTTPException(400, "send a JSON object")
        return data

    @api.get(PREFIX, response_class=HTMLResponse)
    def page():
        # Shown in the window's frame; never inside another site's page.
        return HTMLResponse(checks.page(), headers={"X-Frame-Options": "SAMEORIGIN", "Cache-Control": "no-store"})

    @api.get(f"{PREFIX}/api/jobs")
    def jobs():
        return checks.list_jobs()

    @api.get(PREFIX + "/job/{job_id}/{rel:path}")
    def job_file(job_id: str, rel: str = ""):
        try:
            file = checks.job_file(job_id, rel)
        except PermissionError as e:
            raise HTTPException(403, "forbidden") from e
        except FileNotFoundError as e:
            raise HTTPException(404, "no such job") from e
        if not file.is_file():
            raise HTTPException(404, "not found")
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if rel.startswith("download/"):
            return FileResponse(file, media_type="application/octet-stream", filename=safe_name(file.name),
                                headers=headers)
        return FileResponse(file, media_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream",
                            headers=headers)

    @api.post(f"{PREFIX}/api/check", status_code=201)
    async def check(request: Request):
        window_only(request)
        opts = dict(request.query_params)
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD:
            return JSONResponse({"error": "file too large (1 GB at most)"}, 413)
        incoming = checks.root / "incoming"
        incoming.mkdir(exist_ok=True)
        tmp = incoming / uuid.uuid4().hex
        size = 0
        try:
            with open(tmp, "wb") as out:  # streamed to disk: project files can be large
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        return JSONResponse({"error": "file too large (1 GB at most)"}, 413)
                    out.write(chunk)

            def move(dest: Path) -> int:
                if not size:
                    raise ValueError("the file is empty")
                shutil.move(tmp, dest)
                return size

            return checks.submit_file(opts.pop("name", "upload"), move, opts).public()
        except (ConfigError, ValueError) as e:
            return fail(e)
        finally:
            tmp.unlink(missing_ok=True)

    @api.post(f"{PREFIX}/api/loop", status_code=201)
    async def loop(request: Request):
        form = await body_json(request)
        try:
            return checks.submit_loop(str(form.get("job", "")), int(form.get("loops") or 0)).public()
        except (ConfigError, ValueError) as e:
            return fail(e)

    @api.post(f"{PREFIX}/api/feedback")
    async def feedback(request: Request):
        form = await body_json(request)
        try:
            return checks.feedback(str(form.get("job", "")), str(form.get("check", "")), str(form.get("verdict", "")))
        except ValueError as e:
            return fail(e)

    @api.post(f"{PREFIX}/api/remove")
    async def remove(request: Request):
        form = await body_json(request)
        try:
            checks.remove(str(form.get("job", "")))
        except (FileNotFoundError, ValueError) as e:
            return fail(e)
        return {"removed": True}
