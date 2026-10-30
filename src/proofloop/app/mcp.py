"""``proofloop mcp``: ProofLoop for assistants such as Claude, over the Model Context Protocol (stdio).

An assistant can check parts and G-code (and see the renders), make and check loop files, cost a
plate, plan a run over your printers and keep it as a queue, and read how the printers are doing,
the print history, filament and the summary. It can never start or stop a print: nothing here
sends anything to a printer. Sending stays in the ProofLoop window, with your confirmation.

Each tool is a thin layer over a ``proofloop ... --json`` command (run in its own process, so a
slow or failing check never takes the server down) or over the running ProofLoop's own API, for
the live state of your printers. No extra packages: the protocol is newline-delimited JSON-RPC.

Add it to Claude Code with ``claude mcp add proofloop -- proofloop mcp``; ``proofloop mcp --config``
prints the entry for Claude Desktop's ``claude_desktop_config.json``.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from proofloop import __version__
from proofloop.app.config import DEFAULT_PORT
from proofloop.core.output import dumps

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_IMAGES = 4  # renders and finding pictures returned with a check
INSTRUCTIONS = """\
ProofLoop checks 3D-printable designs and sliced G-code for Bambu Lab printers, makes FarmLoop loop
files (a plate printed many times, swept off between loops), and watches the printers.

- Paths are files on this computer; give absolute paths.
- Check results: `ready` is true when nothing major or worse is left. Each finding has a stable
  `id` (the check), a `severity` (1 note, 2 minor, 3 major, 4 will fail), a `fix`, and where it is.
  To improve a design: change it, check it again, and compare (`since_last_version`).
- Nothing here starts a print. Planned runs go to the queue; the person sends them from the
  ProofLoop window. Never tell them a print has started.
- Live printer state needs ProofLoop running on this computer (it usually is: it starts at login).
"""


def _tool(name: str, description: str, props: dict, required: list[str] | None = None,
          read_only: bool = True) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": required or [],
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": False}}


PATH = {"type": "string", "description": "Absolute path to the file on this computer."}
TOOLS = [
    _tool("check_part", "Check a CAD part (STL, 3MF, OBJ, STEP, or OpenSCAD .scad code) before printing: "
          "printability, strength, fit, stability, and for auto-eject, whether the pusher can clear it. Returns "
          "findings with fixes and the numbers behind them, and renders. Give the same `design` name each time to "
          "see what changed since the last version.",
          {"path": PATH,
           "design": {"type": "string", "description": "A design name (versions are compared) or a design YAML path."},
           "printer": {"type": "string"}, "material": {"type": "string", "description": "PLA, PETG, ABS, TPU..."},
           "units": {"type": "string", "enum": ["mm", "cm", "m", "in"]},
           "handheld": {"type": "boolean"}, "auto_eject": {"type": "boolean"},
           "process": {"type": "string", "enum": ["print", "injection", "cnc"]},
           "images": {"type": "boolean", "description": "Return renders and finding pictures (default true)."}},
          ["path"]),
    _tool("check_gcode", "Check sliced G-code (.gcode or a Bambu .gcode.3mf): plate, temperatures, flow, cooling, "
          "the loop-file steps. Returns findings and the slicer settings that fix them.",
          {"path": PATH, "printer": {"type": "string"}, "material": {"type": "string"},
           "plate": {"type": "integer", "minimum": 1}}, ["path"]),
    _tool("prepare_print", "From CAD to a print-ready verdict: check the design, slice it with Bambu Studio or "
          "OrcaSlicer on this computer, check the G-code, optionally apply the slicer-setting fixes and make a "
          "loop file. Takes a minute or two.",
          {"path": PATH, "printer": {"type": "string"}, "material": {"type": "string"},
           "loops": {"type": "integer", "minimum": 0, "maximum": 50}, "fix": {"type": "boolean"},
           "design": {"type": "string"}}, ["path"], read_only=False),
    _tool("make_loop_file", "Turn a sliced plate (.gcode.3mf) into a loop file with the person's saved settings. "
          "Writes the file next to the plate (or to `output`); it doesn't send it anywhere.",
          {"path": PATH, "loops": {"type": "integer", "minimum": 1, "maximum": 50},
           "output": {"type": "string", "description": "Where to write it (absolute path)."}},
          ["path", "loops"], read_only=False),
    _tool("check_loop_file", "Check any loop file (FarmLoop's or ProofLoop's) for known problems.", {"path": PATH},
          ["path"]),
    _tool("cost_plate", "What a plate costs and earns per part, loop and run, from the person's own prices.",
          {"path": PATH, "loops": {"type": "integer", "minimum": 1}}, ["path"]),
    _tool("plan_run", "Spread a run of N parts from a sliced plate over the printers that can print it, so they "
          "finish together. With keep=true it's kept as a queue the person sends from the window; nothing starts.",
          {"path": PATH, "parts": {"type": "integer", "minimum": 1, "maximum": 100000}, "keep": {"type": "boolean"}},
          ["path", "parts"], read_only=False),
    _tool("farm_status", "Every printer right now: online, what it's printing, loop N of M, when it finishes, "
          "and anything waiting for the person; plus the queue.", {}),
    _tool("summary", "What happened on the printers: parts and loops made, failures with layer and HMS codes, "
          "plates with parts left, filament used and spools running low.",
          {"hours": {"type": "number", "minimum": 1, "maximum": 744}}),
    _tool("print_history", "How recent prints ended (finished, failed, cancelled), with layers, HMS alerts, "
          "temperatures and plate checks.",
          {"limit": {"type": "integer", "minimum": 1, "maximum": 500}, "printer": {"type": "string"}}),
    _tool("filament", "Every spool: material, colour, what's left, how many days it lasts, and what's low.", {}),
    _tool("setup_check", "ProofLoop's setup check: the slicer and its presets, each printer, notifications, "
          "start at login.", {}),
]


PROMPTS = [{
    "name": "improve_part", "title": "Make a part ready to print",
    "description": "Check a part, fix what's found in its design, and check again until it's ready to print.",
    "arguments": [{"name": "path", "description": "The part: absolute path to an STL, 3MF, STEP or .scad file",
                   "required": True},
                  {"name": "goal", "description": "What it's for, and anything that must not change", "required": False}],
}]
IMPROVE = """Make the part at {path} ready to print{goal}.

1. Check it with check_part, using design name "{name}" every time, so each round is compared with the last.
2. Look at the renders. For each finding of severity 3 or 4, change the design so it goes away, starting with
   the worst: use its `fix`, `location` and `measured` numbers. If it's an OpenSCAD file, edit the code; for a
   mesh, say exactly what to change in the CAD tool, and stop there.
3. Check again. Keep going until `ready` is true, or a round fixes nothing (then say why).
4. Finish with what you changed and why, what's left (minor findings and notes), and the estimates
   (grams, hours). Don't send anything to a printer: that's done in the ProofLoop window.
"""


class ToolError(Exception):
    pass


class Server:
    def __init__(self, port: int = DEFAULT_PORT, python: str | None = None):
        self.port = port
        self.python = python or sys.executable

    # ---- the protocol -----------------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:  # a notification (initialized, cancelled...): no answer
            return None
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                          "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                          "serverInfo": {"name": "proofloop", "title": "ProofLoop", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "prompts/list":
                result = {"prompts": PROMPTS}
            elif method == "prompts/get":
                result = self.prompt(msg.get("params") or {})
            elif method == "tools/call":
                params = msg.get("params") or {}
                result = self.call(params.get("name", ""), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
        except Exception as e:  # noqa: BLE001 - the server must answer, whatever happens
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def prompt(self, params: dict) -> dict:
        if params.get("name") != "improve_part":
            raise ValueError(f"no prompt called {params.get('name')!r}")
        args = params.get("arguments") or {}
        path = str(args.get("path") or "").strip()
        if not path:
            raise ValueError("give the path of the part")
        goal = str(args.get("goal") or "").strip()
        text = IMPROVE.format(path=path, goal=f" ({goal})" if goal else "", name=Path(path).stem.replace("_", " "))
        return {"description": PROMPTS[0]["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}

    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, f"t_{name}", None)
        if fn is None or name not in {t["name"] for t in TOOLS}:
            return _error(f"ProofLoop has no tool called {name!r}")
        try:
            data, images = fn(**args)
        except ToolError as e:
            return _error(str(e))
        except TypeError as e:
            return _error(f"wrong arguments for {name}: {e}")
        content: list[dict] = [{"type": "text", "text": dumps(data)}]
        for path in images[:MAX_IMAGES]:
            try:
                content.append({"type": "image", "mimeType": "image/png",
                                "data": base64.b64encode(Path(path).read_bytes()).decode()})
            except OSError:
                pass
        out = {"content": content}
        if isinstance(data, dict):
            out["structuredContent"] = data
        return out

    def serve(self, stdin=None, stdout=None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}}
            else:
                reply = self.handle(msg) if isinstance(msg, dict) else None
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()

    # ---- the commands and the running ProofLoop ---------------------------------------------
    def _run(self, *args: str, timeout: float = 900) -> Any:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            r = subprocess.run([self.python, "-m", "proofloop", *args, "--json"], capture_output=True, timeout=timeout,
                               env=env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"ProofLoop took longer than {timeout / 60:.0f} minutes") from e
        out = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode >= 2 or not out:
            err = r.stderr.decode("utf-8", "replace").strip().splitlines()
            raise ToolError(err[-1] if err else f"proofloop {args[0]} failed (exit {r.returncode})")
        return json.loads(out)

    def _api(self, path: str, body: bytes | None = None, ctype: str | None = None, timeout: float = 60) -> Any:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body)
        token = _token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("detail")
            except ValueError:
                detail = None
            raise ToolError(str(detail or e)) from e
        except OSError as e:
            raise ToolError("ProofLoop isn't running on this computer: start it (the ProofLoop shortcut, or "
                            "`proofloop`), then try again") from e

    @staticmethod
    def _file(path: str) -> str:
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            raise ToolError(f"give an absolute path (got {path!r})")
        if not p.is_file():
            raise ToolError(f"there's no file at {p}")
        return str(p)

    # ---- tools -----------------------------------------------------------------------------
    def t_check_part(self, path: str, design: str | None = None, printer: str | None = None,
                     material: str | None = None, units: str | None = None, handheld: bool = False,
                     auto_eject: bool = False, process: str | None = None, images: bool = True):
        args = ["check", "design", self._file(path)]
        for flag, value in (("--design", design), ("--printer", printer), ("--material", material),
                            ("--units", units), ("--process", process)):
            if value:
                args += [flag, value]
        args += ["--handheld"] * handheld + ["--auto-eject"] * auto_eject + ["--personas", "3"]
        r = self._run(*args)
        pics = []
        if images and r.get("kind") == "design":
            pics = [r["renders"][k] for k in ("iso", "front") if r.get("renders", {}).get(k)]
            pics += [f["image"] for f in sorted(r["findings"], key=lambda f: -(f["severity"] or 0)) if f.get("image")]
        return r, pics

    def t_check_gcode(self, path: str, printer: str | None = None, material: str | None = None,
                      plate: int | None = None):
        args = ["check", "gcode", self._file(path)]
        for flag, value in (("--printer", printer), ("--material", material), ("--plate", plate)):
            if value:
                args += [flag, str(value)]
        r = self._run(*args)
        return r, [f["image"] for f in r.get("findings", []) if f.get("image")]

    def t_prepare_print(self, path: str, printer: str | None = None, material: str | None = None,
                        loops: int = 0, fix: bool = False, design: str | None = None):
        args = ["check", "prepare", self._file(path), "--loops", str(loops)]
        for flag, value in (("--printer", printer), ("--material", material), ("--design", design)):
            if value:
                args += [flag, value]
        return self._run(*args, *(["--fix"] if fix else [])), []

    def t_make_loop_file(self, path: str, loops: int, output: str | None = None):
        args = ["loop", "make", self._file(path), "--loops", str(loops)]
        if output:
            if not Path(output).is_absolute():
                raise ToolError("give `output` as an absolute path")
            args += ["-o", output]
        return self._run(*args), []

    def t_check_loop_file(self, path: str):
        return self._run("loop", "check", self._file(path)), []

    def t_cost_plate(self, path: str, loops: int | None = None):
        return self._run("loop", "cost", self._file(path), *(["--loops", str(loops)] if loops else [])), []

    def t_plan_run(self, path: str, parts: int, keep: bool = False):
        p = Path(self._file(path))
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{p.name}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + p.read_bytes() + \
            f"\r\n--{boundary}--\r\n".encode()
        plate = self._api("/api/plates", body, f"multipart/form-data; boundary={boundary}", timeout=120)
        req = json.dumps({"token": plate["token"], "parts": parts, "settings": {}}).encode()
        plan = self._api(f"/api/plates/{plate['token']}/spread", req, "application/json")
        out = {"plate": p.name, "plan": plan, "kept": None}
        if keep:
            out["kept"] = self._api("/api/queue", req, "application/json")
            out["note"] = "Kept as a queue under Printers › Overview. Nothing starts until the person sends each job."
        return out, []

    def t_farm_status(self):
        o = self._api("/api/overview")
        return {"printers": o["runs"], "today": o["today"], "queue": self._api("/api/queue"),
                "low_spools": o["low_spools"], "latest": o["events"][:8]}, []

    def t_summary(self, hours: float = 12):
        try:
            return self._api(f"/api/summary?hours={hours}"), []
        except ToolError:
            return self._run("summary", "--hours", str(hours)), []

    def t_print_history(self, limit: int = 30, printer: str | None = None):
        rows = self._run("printers", "history", "--limit", str(limit * (5 if printer else 1)))
        if printer:
            rows = [r for r in rows if r.get("printer") == printer][:limit]
        return {"prints": rows}, []

    def t_filament(self):
        return self._run("printers", "spools"), []

    def t_setup_check(self):
        return self._run("doctor", "--port", str(self.port)), []


def _token() -> str | None:
    """The access token, when you've set one (``app_token`` in settings.json)."""
    from proofloop.core.paths import home
    from proofloop.core.settings import SettingsStore

    try:
        return SettingsStore(home()).load().app_token
    except (OSError, ValueError):
        return None


def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def config_entry(python: str | None = None) -> dict:
    """The entry for Claude Desktop's claude_desktop_config.json, with this computer's own paths."""
    return {"mcpServers": {"proofloop": {"command": python or sys.executable, "args": ["-m", "proofloop", "mcp"]}}}


def main(port: int = DEFAULT_PORT) -> int:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    Server(port).serve()
    return 0
