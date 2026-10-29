"""The app window: one local page to drop a CAD or G-code file, or paste a website and a task.

Jobs run in the background and the page shows them as they progress; each finished job links
to its report. The server binds to localhost only, accepts requests only for its own address
(no DNS rebinding) and only POSTs that carry its own header (other web pages cannot drive it).
Adding a site to the allowlist happens only when the user ticks the box for that site.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from proofloop.check.feedback import path as feedback_path
from proofloop.core import theme
from proofloop.core.config import ConfigError, Settings, load_settings, load_yaml_file
from proofloop.core.db import Database
from proofloop.core.logging import get_logger
from proofloop.core.paths import config_file

log = get_logger(__name__)

MAX_UPLOAD = 1024 * 1024 * 1024          # 1 GB: large Bambu project files
CAD_SUFFIXES = (".stl", ".3mf", ".obj", ".ply", ".glb", ".gltf", ".off", ".step", ".stp")
GCODE_SUFFIXES = (".gcode", ".gcode.3mf", ".gco", ".bgcode")
HEADER = "X-ProofLoop"


NOTIFY_AFTER_S = 30  # only jobs that take this long are worth a phone alert


@dataclass
class Job:
    id: str
    kind: str                      # cad | gcode | web
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
        data["report_url"] = f"/job/{self.id}/" if self.report else None
        data["can_loop"] = bool(self.source)
        data["download"] = f"/job/{self.id}/download/{Path(self.download).name}" if self.download else None
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


LOCAL_ORIGIN = re.compile(r"http://(127\.0\.0\.1|localhost|\[::1\]):\d{1,5}")


class App:
    def __init__(self, settings: Settings, db: Database, port: int, shell: str | None = None):
        if shell is not None and not LOCAL_ORIGIN.fullmatch(shell):
            raise ValueError("the app can only be shown inside a page on this computer")
        self.settings = settings
        self.db = db
        self.port = port
        self.shell = shell  # origin of the ProofLoop window that shows this app in a frame, if any
        self.root = (settings.data_dir / "checks").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.feedback_file = feedback_path()
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.checks = ThreadPoolExecutor(max_workers=2, thread_name_prefix="proofloop-check")
        self.browser = ThreadPoolExecutor(max_workers=1, thread_name_prefix="proofloop-web")  # one site test at a time
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
                job.status, job.error = "failed", "the app was closed before this finished; run it again"
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
        self.browser.shutdown(wait=False, cancel_futures=True)

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
    def page(self, view: str = "") -> str:
        from proofloop.check.my_printers import farm_printers

        data = load_yaml_file(config_file(self.settings.config_dir, "printers.yaml")) or {}
        printers = [{"id": k, "label": (v.get("names") or [k])[0], "type": v.get("type", "fdm")}
                    for k, v in (data.get("printers") or {}).items()]
        farm = farm_printers(data)  # the printers you added under Printers: one list for everything
        materials = list((data.get("materials") or {}).keys())
        procs = load_yaml_file(config_file(self.settings.config_dir, "processes.yaml")) or {}
        process_materials = {"print": materials, **{k: list((v or {}).get("materials", {})) for k, v in procs.items()}}
        hosts = sorted({f"{a.host}" + (f":{','.join(map(str, a.ports))}" if a.ports else "")
                        for a in self.settings.safety.allowlist})
        smart = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        try:
            from proofloop.check.slicer import find_slicer

            found = find_slicer()
            slicer = found.label if found and found.profile_dirs else None
        except Exception:  # noqa: BLE001 - a broken slicer install just hides the option
            slicer = None
        return self.env.get_template("check_app.html.j2").render(
            printers=printers, materials=materials, default_printer=data.get("default_printer"),
            process_materials=process_materials, slicer=slicer,
            default_material=data.get("default_material"), hosts=hosts, smart=smart, header=HEADER,
            devices=sorted(self.settings.devices), shell=self.shell, farm=farm,
            default_farm=farm[0]["id"] if farm else None, view=view if view in ("print", "web") else "")

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
            log.exception("app job %s failed", job.id)
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

    def submit_test(self, form: dict[str, Any]) -> Job:
        from proofloop.web.quick import build_study
        from proofloop.web.safety.allow_edit import add_to_allowlist, host_entry
        from proofloop.web.safety.allowlist import SafetyError, check_url

        url = str(form.get("url", "")).strip()
        tasks = [str(t).strip() for t in form.get("tasks", []) if str(t).strip()]
        successes = [str(t).strip() for t in form.get("successes", []) if str(t).strip()]
        personas = max(1, min(20, int(form.get("personas") or 5)))
        repeats = max(1, min(5, int(form.get("repeats") or 2)))
        host, _ = host_entry(url)
        settings = self.settings
        try:
            check_url(url, settings.safety.allowlist)
        except SafetyError:
            if not form.get("allow"):
                raise PermissionError(f"{host} is not on the safety allowlist. If it is your own test site, tick "
                                      "'This is my own site' to add it (payments, emails and deletes stay blocked).") from None
            add_to_allowlist(settings.config_dir, url, note="added in the ProofLoop window")
            self.settings = settings = load_settings(settings.config_dir)
        if form.get("smart"):
            settings, _ = settings.smart()
        study, raw = build_study(settings, url, tasks, successes, find=bool(form.get("find")), personas=personas,
                                  repeats=repeats, device=form.get("device") or None)
        job = self._new("web", f"{host}: {tasks[0][:60]}")
        total = personas * len(study.tasks) * repeats * study.visits
        work = lambda j: self._web_work(j, settings, study, raw, personas, repeats, total)  # noqa: E731
        self.browser.submit(self._run, job, work)
        return job

    def _web_work(self, job: Job, settings: Settings, study: Any, raw: dict[str, Any], personas: int, repeats: int,
                  total: int) -> None:
        from proofloop.web.quick import run_quick_test, save_study

        done = {"n": 0, "ok": 0}

        def on_session(result: Any) -> None:
            done["n"] += 1
            done["ok"] += result.outcome == "completed"
            job.progress = min(1.0, done["n"] / max(total, 1))
            job.stage = f"personas are using the site ({done['n']} of {total} sessions)"

        def on_stage(msg: str) -> None:
            if not msg.startswith("personas"):
                job.stage = msg

        from proofloop.web.runner.browser import ensure_chromium

        ensure_chromium(settings, lambda msg: setattr(job, "stage", msg))
        save_study(settings, study, raw)
        run_id, report, _est = asyncio.run(run_quick_test(settings, self.db, study, personas=personas, repeats=repeats,
                                                          approve=lambda _e: True, on_session=on_session,
                                                          on_stage=on_stage))
        job.report = str(report)
        promoted = self.db.all("SELECT title FROM issue_clusters WHERE run_id = ? AND promoted = 1 ORDER BY score DESC",
                               [run_id])
        job.counts = {"total": len(promoted), "completed": done["ok"], "sessions": done["n"]}
        job.summary = [f"{done['ok']} of {done['n']} sessions finished the task"] + [p["title"] for p in promoted[:4]]

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


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{app.port}", f"localhost:{app.port}", f"[::1]:{app.port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "proofloop"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("app: " + fmt, *args)

        def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", status: int = 200,
                  attachment: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{safe_name(attachment)}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if app.shell:  # shown inside the ProofLoop window, and nowhere else
                self.send_header("Content-Security-Policy", f"frame-ancestors {app.shell}")
            else:
                self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            self._send(json.dumps(data).encode("utf-8"), "application/json", status)

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._send(b"wrong host", "text/plain", 403)
            path = unquote(urlsplit(self.path).path)
            try:
                if path in ("/", "/index.html"):
                    view = (parse_qs(urlsplit(self.path).query).get("view") or [""])[0]
                    return self._send(app.page(view).encode("utf-8"))
                if path == "/api/jobs":
                    return self._json(app.list_jobs())
                if path.startswith("/job/"):
                    parts = path.split("/", 3)
                    file = app.job_file(parts[2], parts[3] if len(parts) > 3 else "")
                    if not file.is_file():
                        return self._send(b"not found", "text/plain", 404)
                    if "/download/" in path:
                        return self._send(file.read_bytes(), "application/octet-stream", attachment=file.name)
                    return self._send(file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")
                return self._send(b"not found", "text/plain", 404)
            except PermissionError:
                return self._send(b"forbidden", "text/plain", 403)
            except FileNotFoundError:
                return self._send(b"no such job", "text/plain", 404)

        def do_POST(self) -> None:  # noqa: N802
            origin = self.headers.get("Origin")
            if not self._host_ok() or self.headers.get(HEADER) != "1" or \
                    (origin and urlsplit(origin).netloc not in allowed_hosts):
                return self._json({"error": "requests must come from the app window"}, 403)
            url = urlsplit(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_UPLOAD:
                return self._json({"error": "file too large (1 GB at most)"}, 413)
            try:
                if url.path == "/api/check":
                    opts = {k: v[0] for k, v in parse_qs(url.query).items()}
                    job = app.submit_file(opts.pop("name", "upload"), lambda dest: self._save_body(dest, length), opts)
                    return self._json(job.public(), 201)
                if url.path == "/api/loop":
                    form = json.loads(self.rfile.read(length) or b"{}")
                    return self._json(app.submit_loop(str(form.get("job", "")), int(form.get("loops") or 0)).public(), 201)
                if url.path in ("/api/feedback", "/api/remove"):
                    form = json.loads(self.rfile.read(length) or b"{}")
                    if url.path == "/api/remove":
                        app.remove(str(form.get("job", "")))
                        return self._json({"removed": True})
                    return self._json(app.feedback(str(form.get("job", "")), str(form.get("check", "")),
                                                   str(form.get("verdict", ""))))
                if url.path == "/api/test":
                    form = json.loads(self.rfile.read(length) or b"{}")
                    return self._json(app.submit_test(form).public(), 201)
                return self._json({"error": "not found"}, 404)
            except PermissionError as exc:
                return self._json({"error": str(exc), "needs_allow": True}, 400)
            except (ConfigError, ValueError) as exc:
                return self._json({"error": str(exc)}, 400)
            except FileNotFoundError:
                return self._json({"error": "no such job"}, 404)
            except Exception as exc:  # noqa: BLE001
                from proofloop.web.safety.allowlist import SafetyError

                if isinstance(exc, SafetyError):
                    return self._json({"error": str(exc)}, 400)
                log.exception("app request failed")
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def _save_body(self, dest: Path, length: int) -> int:
            left = length
            with open(dest, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ValueError("the upload was cut off")
                    f.write(chunk)
                    left -= len(chunk)
            if length == 0:
                raise ValueError("the file is empty")
            return length

    return Handler


def serve(settings: Settings, db: Database, port: int = 8700, host: str = "127.0.0.1",
          shell: str | None = None) -> tuple[ThreadingHTTPServer, App]:
    """``shell`` is the origin of a ProofLoop window allowed to show the app in a frame."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the app only binds to localhost")
    app = App(settings, db, port, shell)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    app.port = server.server_address[1]          # port 0 = any free port (tests)
    server.RequestHandlerClass = make_handler(app)
    return server, app


def app_browser() -> str | None:
    """Chrome or Edge, which can show a page as its own window (--app) without tabs or an address bar."""
    candidates: list[str] = []
    if sys.platform == "win32":
        for base in filter(None, (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                                  os.environ.get("LOCALAPPDATA"))):
            candidates += [str(Path(base) / "Microsoft/Edge/Application/msedge.exe"),
                           str(Path(base) / "Google/Chrome/Application/chrome.exe")]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                       "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    for exe in candidates:
        if Path(exe).is_file():
            return exe
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_window(url: str, profile_dir: Path) -> bool:
    """Open the app in its own window; falls back to a normal browser tab."""
    exe = app_browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1120,880", f"--user-data-dir={profile_dir}",
                              "--no-first-run", "--no-default-browser-check"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    import webbrowser

    return webbrowser.open(url)
