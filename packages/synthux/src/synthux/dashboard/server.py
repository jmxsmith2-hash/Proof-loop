"""Local, read-only dashboard over the database (http://127.0.0.1:8800 by default).

Lists runs, opens their reports (built on first view), shows calibration history
and physical-product predictions. Binds to localhost only.
"""

from __future__ import annotations

import asyncio
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from jinja2 import Environment, PackageLoader, select_autoescape

from synthux import SYNTHETIC_DISCLAIMER
from synthux.calibration.compare import history
from synthux.core.config import Settings
from synthux.core.db import Database
from synthux.core.logging import get_logger
from synthux.report.charts import legend, line_chart

log = get_logger(__name__)


class Dashboard:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.reports = (settings.data_dir / "reports").resolve()
        self.runs_dir = (settings.data_dir / "runs").resolve()   # session screenshots
        self.env = Environment(loader=PackageLoader("synthux", "dashboard/templates"),
                               autoescape=select_autoescape(["j2"]))
        self._lock = threading.Lock()

    # --------------------------------------------------------------- data
    def runs(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.all("SELECT * FROM runs ORDER BY created_at DESC"):
            stats = self.db.one("SELECT COUNT(*) AS n, SUM(outcome = 'completed') AS done FROM sessions WHERE run_id = ?",
                                [r["id"]])
            promoted = self.db.one("SELECT COUNT(*) AS n FROM issue_clusters WHERE run_id = ? AND promoted = 1",
                                   [r["id"]])["n"]
            top = self.db.one("SELECT title, screen_id FROM issue_clusters WHERE run_id = ? AND promoted = 1 "
                              "ORDER BY score DESC LIMIT 1", [r["id"]])
            out.append({**r, "sessions": stats["n"], "completion": (stats["done"] or 0) / stats["n"] if stats["n"] else None,
                        "promoted": promoted, "top": top,
                        "has_report": (self.reports / r["id"] / "index.html").is_file()})
        return out

    def page(self) -> str:
        cal = history(self.db)
        chart = ""
        if len(cal) >= 2:
            series = [{"name": "agreement", "values": [round(h["agreement"] * 100, 1) for h in cal], "slot": 0},
                      {"name": "issue F1", "values": [round(h["f1"] * 100, 1) for h in cal], "slot": 1}]
            chart = legend(series) + line_chart(series, width=900, height=220, y_max=100,
                                                title="agreement with real users over time")
        physical = self.db.all("SELECT * FROM physical_products ORDER BY created_at DESC")
        designs = []
        for d in self.db.all("SELECT * FROM designs ORDER BY created_at DESC"):
            versions = self.db.all("SELECT * FROM design_versions WHERE design_id = ? ORDER BY version", [d["id"]])
            for v in versions:
                sev = [f["severity"] for f in v["findings_json"]]
                rel = Path(v["report_path"] or "")
                try:
                    v["link"] = "/reports/" + rel.resolve().relative_to(self.reports).as_posix()
                except ValueError:
                    v["link"] = None
                v["counts"] = {"total": len(sev), "fail": sev.count(4), "major": sev.count(3)}
                v["prints"] = self.db.one("SELECT COUNT(*) AS n FROM print_results WHERE version_id = ?", [v["id"]])["n"]
            designs.append({**d, "versions": versions})
        return self.env.get_template("index.html.j2").render(
            runs=self.runs(), calibration=cal, cal_chart=chart, physical=physical, designs=designs,
            disclaimer=SYNTHETIC_DISCLAIMER,
            profiles=self.db.all("SELECT version, parent, created_at, note FROM calibration_profiles ORDER BY created_at"))

    # ----------------------------------------------------------- sessions
    def _labels(self, persona_ids: set[str]) -> dict[str, str]:
        if not persona_ids:
            return {}
        marks = ",".join("?" for _ in persona_ids)
        return {r["id"]: r["label"] for r in self.db.all(f"SELECT id, label FROM personas WHERE id IN ({marks})",
                                                           sorted(persona_ids))}

    def sessions_page(self, run_id: str) -> str:
        run = self.db.one("SELECT * FROM runs WHERE id = ?", [run_id])
        if run is None:
            raise FileNotFoundError(run_id)
        sessions = self.db.all("SELECT * FROM sessions WHERE run_id = ? ORDER BY task_id, persona_id, repeat_index, "
                               "visit_index", [run_id])
        labels = self._labels({s["persona_id"] for s in sessions})
        counts = {r["session_id"]: r["n"] for r in self.db.all(
            "SELECT session_id, COUNT(*) AS n FROM issues WHERE run_id = ? AND status = 'accepted' GROUP BY session_id",
            [run_id])}
        rows = [{**s, "persona": labels.get(s["persona_id"], s["persona_id"]),
                 "device": (s["conditions_json"] or {}).get("device") or "own", "issues": counts.get(s["id"], 0)}
                for s in sessions]
        return self.env.get_template("sessions.html.j2").render(
            title=f"Sessions of {run_id}", run=run, sessions=rows, disclaimer=SYNTHETIC_DISCLAIMER, back=None)

    def session_page(self, session_id: str) -> str:
        from synthux.elicitation.questions import PROBLEM_SIGNALS

        session = self.db.one("SELECT * FROM sessions WHERE id = ?", [session_id])
        if session is None:
            raise FileNotFoundError(session_id)
        steps = []
        for st in self.db.all("SELECT * FROM steps WHERE session_id = ? ORDER BY idx", [session_id]):
            intended, executed = st["intended_json"] or {}, st["action_json"] or {}
            signals = st["signals_json"] or {}
            slip = ""
            if signals.get("noise_applied") and executed:
                slip = "misclick" if (executed.get("x"), executed.get("y")) != (intended.get("x"), intended.get("y")) \
                    else "typo"
            steps.append({"idx": st["idx"], "screen_id": st["screen_id"], "think": st["think_json"] or {},
                          "action": _describe(intended), "slip": slip, "frustration": st["frustration"],
                          "signals": [k for k, v in signals.items() if v is True and k != "noise_applied"],
                          "shot": self.shot_link(st["screenshot_path"])})
        issues = self.db.all("SELECT * FROM issues WHERE session_id = ? AND failure_class = 'UX_ISSUE' "
                             "ORDER BY step_idx", [session_id])
        persona = self._labels({session["persona_id"]}).get(session["persona_id"], session["persona_id"])
        return self.env.get_template("session.html.j2").render(
            title=f"{persona} · {session['task_id']}", session=session, persona=persona, steps=steps, issues=issues,
            device=(session["conditions_json"] or {}).get("device") or "persona's own device",
            bad_signals=set(PROBLEM_SIGNALS), disclaimer=SYNTHETIC_DISCLAIMER,
            back={"href": f"/run/{session['run_id']}/sessions", "label": "all sessions of this run"})

    def shot_link(self, path: str | None) -> str | None:
        if not path:
            return None
        try:
            return "/shots/" + Path(path).resolve().relative_to(self.runs_dir).as_posix()
        except ValueError:
            return None

    def resolve_shot(self, rel: str) -> Path:
        target = (self.runs_dir / rel).resolve()
        if self.runs_dir not in target.parents:
            raise PermissionError(rel)
        return target

    # ------------------------------------------------------------ reports
    def ensure_report(self, run_id: str) -> Path:
        path = self.reports / run_id / "index.html"
        if not path.is_file():
            if not self.db.one("SELECT id FROM runs WHERE id = ?", [run_id]):
                raise FileNotFoundError(run_id)
            from synthux.report.build import ReportBuilder

            with self._lock:
                asyncio.run(ReportBuilder(self.settings, self.db).build(run_id))
        return path

    def resolve(self, rel: str) -> Path:
        """Map a URL path under /reports/ to a file, refusing anything outside the reports folder."""
        target = (self.reports / rel).resolve()
        if self.reports not in target.parents and target != self.reports:
            raise PermissionError(rel)
        return target


def _describe(action: dict[str, Any]) -> str:
    kind = action.get("kind", "")
    target = action.get("target") or ""
    if kind == "fill":
        return f"types into '{target}'"
    if kind == "scroll":
        return "scrolls " + ("down" if (action.get("dy") or 0) >= 0 else "up")
    if kind == "press":
        return f"presses {action.get('key') or 'a key'}"
    return f"{kind} '{target}'" if target else kind


def make_handler(dash: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "synthux-dashboard"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("dashboard: " + fmt, *args)

        def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = unquote(urlsplit(self.path).path)
            try:
                if path in ("/", "/index.html"):
                    return self._send(dash.page().encode("utf-8"))
                if path.startswith("/run/") and path.rstrip("/").endswith("/sessions"):
                    return self._send(dash.sessions_page(path.split("/")[2]).encode("utf-8"))
                if path.startswith("/session/"):
                    return self._send(dash.session_page(path.split("/")[2]).encode("utf-8"))
                if path.startswith("/shots/"):
                    file = dash.resolve_shot(path[len("/shots/"):])
                    if not file.is_file():
                        return self._send(b"not found", "text/plain", 404)
                    ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    return self._send(file.read_bytes(), ctype)
                if path.startswith("/run/"):
                    run_id = path.split("/")[2]
                    dash.ensure_report(run_id)
                    self.send_response(302)
                    self.send_header("Location", f"/reports/{run_id}/index.html")
                    self.end_headers()
                    return
                if path.startswith("/reports/"):
                    file = dash.resolve(path[len("/reports/"):])
                    if file.is_dir():
                        file = file / "index.html"
                    if not file.is_file():
                        return self._send(b"not found", "text/plain", 404)
                    ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    return self._send(file.read_bytes(), ctype)
                return self._send(b"not found", "text/plain", 404)
            except PermissionError:
                return self._send(b"forbidden", "text/plain", 403)
            except FileNotFoundError:
                return self._send(b"no such run or session", "text/plain", 404)

    return Handler


def serve(settings: Settings, db: Database, port: int = 8800, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the dashboard only binds to localhost")
    return ThreadingHTTPServer((host, port), make_handler(Dashboard(settings, db)))
