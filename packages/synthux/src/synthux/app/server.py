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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from jinja2 import Environment, PackageLoader, select_autoescape

from synthux.core.config import ConfigError, Settings, load_settings, load_yaml_file
from synthux.core.db import Database
from synthux.core.logging import get_logger

log = get_logger(__name__)

MAX_UPLOAD = 1024 * 1024 * 1024          # 1 GB: large Bambu project files
CAD_SUFFIXES = (".stl", ".3mf", ".obj", ".ply", ".glb", ".gltf", ".off", ".step", ".stp")
GCODE_SUFFIXES = (".gcode", ".gcode.3mf", ".gco", ".bgcode")
HEADER = "X-Synthux"


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

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("report")
        data["report_url"] = f"/job/{self.id}/" if self.report else None
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


class App:
    def __init__(self, settings: Settings, db: Database, port: int):
        self.settings = settings
        self.db = db
        self.port = port
        self.root = (settings.data_dir / "app").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.checks = ThreadPoolExecutor(max_workers=2, thread_name_prefix="synthux-check")
        self.browser = ThreadPoolExecutor(max_workers=1, thread_name_prefix="synthux-web")  # one site test at a time
        self.env = Environment(loader=PackageLoader("synthux", "dashboard/templates"),
                               autoescape=select_autoescape(["j2"]))

    # ------------------------------------------------------------------ page
    def page(self) -> str:
        data = load_yaml_file(self.settings.config_dir / "printers.yaml") or {}
        printers = [{"id": k, "label": (v.get("names") or [k])[0], "type": v.get("type", "fdm")}
                    for k, v in (data.get("printers") or {}).items()]
        materials = list((data.get("materials") or {}).keys())
        procs = load_yaml_file(self.settings.config_dir / "processes.yaml") or {}
        process_materials = {"print": materials, **{k: list((v or {}).get("materials", {})) for k, v in procs.items()}}
        hosts = sorted({f"{a.host}" + (f":{','.join(map(str, a.ports))}" if a.ports else "")
                        for a in self.settings.safety.allowlist})
        smart = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        return self.env.get_template("app.html.j2").render(
            printers=printers, materials=materials, default_printer=data.get("default_printer"),
            process_materials=process_materials,
            default_material=data.get("default_material"), hosts=hosts, smart=smart, header=HEADER,
            devices=sorted(self.settings.devices))

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.lock:
            return [j.public() for j in sorted(self.jobs.values(), key=lambda j: -j.created)]

    # ------------------------------------------------------------------ jobs
    def _new(self, kind: str, title: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, title=title)
        with self.lock:
            self.jobs[job.id] = job
        return job

    def _run(self, job: Job, work: Callable[[Job], None]) -> None:
        job.status, job.stage = "running", "starting"
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

    def submit_file(self, filename: str, body_reader: Callable[[Path], int], options: dict[str, str]) -> Job:
        name = safe_name(filename)
        kind = options.get("kind") or file_kind(name)
        if kind not in ("cad", "gcode"):
            raise ValueError(f"{name}: drop a CAD file ({', '.join(CAD_SUFFIXES)}) or sliced G-code "
                             f"({', '.join(GCODE_SUFFIXES)})")
        job = self._new(kind, name)
        folder = self.root / job.id
        folder.mkdir(parents=True)
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
        from synthux.personas.generate import PersonaGenerator
        from synthux.physical.cad.pipeline import check_design, design_spec

        job.stage = "measuring the geometry"
        spec = design_spec(opt.get("design") or None, path)
        if opt.get("handheld") in ("1", "true", "on"):
            spec.use["handheld"] = True
        people = asyncio.run(PersonaGenerator(seed=11).generate(5, include_accessibility=True))
        process = opt.get("process") if opt.get("process") in ("injection", "cnc") else None
        if process:
            job.stage = {"injection": "checking it for injection moulding", "cnc": "checking it for CNC machining"}[process]
        result = check_design(self.settings, self.db, path, spec, people, path.parent / "report",
                              opt.get("printer") or None, opt.get("material") or None, opt.get("units") or None,
                              process=process, pull=opt.get("pull") or None)
        job.report = result["report"]
        job.title = f"{result['design']} v{result['version']}"
        self._summarise(job, result["findings"])
        est = result["metrics"]["estimates"]
        if result["process"]["kind"] == "print":
            printer = (result["printer"].get("names") or [result["printer"]["name"]])[0]
            job.summary.append(f"about {est['printed_grams']} g, {est['print_hours']} h on {printer}")
        else:
            job.summary.append(f"{result['process']['label']} in {result['material']['name']}: " +
                               "; ".join(f"{label} {value}" for value, label in est.get("process_stats", [])[:3]))

    def _gcode_work(self, job: Job, path: Path, opt: dict[str, str]) -> None:
        from synthux.physical.gcode.pipeline import check_gcode

        job.stage = "reading every move"
        result = check_gcode(self.settings.config_dir, path, printer=opt.get("printer") or None,
                             material=opt.get("material") or None,
                             plate=int(opt["plate"]) if (opt.get("plate") or "").isdigit() else None,
                             out=path.parent / "report")
        job.report = result["report"]
        self._summarise(job, result["findings"])
        when = result["time"]["slicer"] or result["time"]["replay"]
        m = result["metrics"]
        if m.get("loop_file"):
            lf = m["loop_file"]
            job.summary.append(f"loop file by {lf['made_by']}: {lf['loops']} loops, {m.get('filament_g_per_loop', 0)} g "
                               f"per loop, printing {when}"
                               + ("" if lf["clearloop_checks"] is not None else " (ejection cycle not checked: "
                                                                               "install ClearLoop)"))
        else:
            job.summary.append(f"{result['printer']['label']}, {result['material']['name']}: "
                               f"{m.get('filament_g', 0)} g, {when}")

    @staticmethod
    def _summarise(job: Job, findings: list[dict[str, Any]]) -> None:
        job.counts = {"fail": sum(f["severity"] == 4 for f in findings), "major": sum(f["severity"] == 3 for f in findings),
                      "total": len(findings)}
        job.summary = [f"{f['severity_name']}: {f['title']}" for f in findings[:4]]
        if not findings:
            job.summary = ["no problems found"]

    def submit_test(self, form: dict[str, Any]) -> Job:
        from synthux.quick import build_study
        from synthux.safety.allow_edit import add_to_allowlist, host_entry
        from synthux.safety.allowlist import SafetyError, check_url

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
                                      "'This is my own site' to add it (payments, emails and deletes stay blocked).")
            add_to_allowlist(settings.config_dir, url, note="added in the synthux app")
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
        from synthux.quick import run_quick_test, save_study

        done = {"n": 0, "ok": 0}

        def on_session(result: Any) -> None:
            done["n"] += 1
            done["ok"] += result.outcome == "completed"
            job.progress = min(1.0, done["n"] / max(total, 1))
            job.stage = f"personas are using the site ({done['n']} of {total} sessions)"

        def on_stage(msg: str) -> None:
            if not msg.startswith("personas"):
                job.stage = msg

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
    def job_file(self, job_id: str, rel: str) -> Path:
        job = self.jobs.get(job_id)
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
        server_version = "synthux-app"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("app: " + fmt, *args)

        def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
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
                    return self._send(app.page().encode("utf-8"))
                if path == "/api/jobs":
                    return self._json(app.list_jobs())
                if path.startswith("/job/"):
                    parts = path.split("/", 3)
                    file = app.job_file(parts[2], parts[3] if len(parts) > 3 else "")
                    if not file.is_file():
                        return self._send(b"not found", "text/plain", 404)
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
                if url.path == "/api/test":
                    form = json.loads(self.rfile.read(length) or b"{}")
                    return self._json(app.submit_test(form).public(), 201)
                return self._json({"error": "not found"}, 404)
            except PermissionError as exc:
                return self._json({"error": str(exc), "needs_allow": True}, 400)
            except (ConfigError, ValueError) as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001
                from synthux.safety.allowlist import SafetyError

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


def serve(settings: Settings, db: Database, port: int = 8700, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, App]:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the app only binds to localhost")
    app = App(settings, db, port)
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
