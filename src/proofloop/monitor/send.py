"""Send a loop file to a printer and start it, when you've turned that on and confirm each time.

Off by default (``send_to_printer`` in settings.json). Before anything is sent, ProofLoop checks:

* the printer is connected, the same model the file was sliced for, and not busy (idle, finished
  or failed; never printing, preparing or paused);
* the loop-file checks find nothing dangerous in the file;
* every filament slot the file uses has a matching spool loaded (same type, nearest colour), so
  the printer is told which AMS tray feeds which slot;
* each tray has enough for the run, where that is known (monitor/runout.py): a tray that runs out
  part-way is a note saying after which loop to swap its spool, and only a tray known to be short
  for the first loop stops the send;
* you confirmed this printer for this file (the request names the printer twice).

The file goes over the printer's own FTPS (implicit TLS on port 990, user ``bblp``, the LAN access
code) to its storage, then ``project_file`` over MQTT starts it. Both follow Bambuddy and the
Bambu Studio LAN protocol (see docs/sources.md). A P2S takes the file in ``cache/`` and only the
newer form of the command (``ftp:///cache/<file>``, the plate's MD5, 0/1 flags, non-zero ids and
``ams_mapping2``): the older ``file:///sdcard/<file>`` form fails there with error 0500-4002,
"unsupported file path or name". This path has not been tried on every model, so the first send
to a new printer is worth watching.
"""

from __future__ import annotations

import ftplib
import hashlib
import io
import re
import socket
import ssl
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from proofloop.monitor.filament import Spool
from proofloop.monitor.runout import WARN, by_tray, forecast

BUSY = ("RUNNING", "PREPARE", "PAUSE", "SLICING")
CACHE_MODELS = {"N7"}  # P2S: files go in cache/ and start with the newer project_file command
EXTERNAL_SPOOL = 254


class SendError(ValueError):
    """The file can't be sent. The message says why, in words for the window."""


@dataclass
class Plan:
    """What will be sent where: the printer, the file name on it, and which tray feeds which slot."""

    printer: str
    filename: str
    plate: int = 1
    use_ams: bool = True
    ams_mapping: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    filament: list[dict] = field(default_factory=list)  # whether each tray lasts the run (monitor/runout.py)


def _hex_rgb(colour: str) -> tuple[int, int, int] | None:
    c = (colour or "").strip().lstrip("#")
    if len(c) < 6:
        return None
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return None


def _distance(a: str, b: str) -> float:
    ra, rb = _hex_rgb(a), _hex_rgb(b)
    if ra is None or rb is None:
        return 0.0 if ra is rb else 255.0  # an unknown colour matches only an unknown colour
    return sum((x - y) ** 2 for x, y in zip(ra, rb, strict=True)) ** 0.5


COLOUR_TOLERANCE = 60.0  # RGB distance: the same filament colour as the slicer and AMS report it


def match_trays(slots: list[tuple[str, str, str]], trays: list[dict]) -> tuple[list[int], list[str]]:
    """For each slot the file uses (id, type, colour), the AMS tray to feed it.

    Returns (mapping indexed by slot id - 1, with -1 for slots the file doesn't use; problems). A
    slot matches a loaded tray of the same type whose colour is close; the closest wins.
    """
    loaded = [t for t in trays if not t.get("empty") and t.get("type")]
    width = max((int(s[0]) for s in slots if str(s[0]).isdigit()), default=0)
    mapping, problems = [-1] * width, []
    for sid, ftype, colour in slots:
        same = [t for t in loaded if str(t["type"]).upper() == str(ftype).upper()]
        best = min(same, key=lambda t: _distance(colour, t.get("color", "")), default=None)
        if best is None or _distance(colour, best.get("color", "")) > COLOUR_TOLERANCE:
            problems.append(f"no {ftype} {colour or ''} loaded for slot {sid}".replace("  ", " "))
            continue
        if str(sid).isdigit():
            mapping[int(sid) - 1] = int(best["tray"])
    return mapping, problems


def feeds(slots: list[tuple[str, str, str]], trays: list[dict]) -> dict[str, int]:
    """Slot id -> the tray a send would feed it from: the AMS mapping, or the external spool without an AMS."""
    if any(not t.get("empty") for t in trays):
        mapping = match_trays(slots, trays)[0]
        return {str(i + 1): t for i, t in enumerate(mapping) if t >= 0}
    return {str(s[0]): EXTERNAL_SPOOL for s in slots} if len(slots) <= 1 else {}


def plan(name: str, state, model_id: str, file_model_id: str, slots: list[tuple[str, str, str]],
         danger: list[str], plate: int = 1, nozzle: str = "", nozzle_flow: str = "",
         run: list[dict[str, float]] | None = None, spools: list[Spool] | None = None, hold: bool = True) -> Plan:
    """Everything that must be true before a file goes to ``name``. Raises SendError with all reasons."""
    result, reasons = check(name, state, model_id, file_model_id, slots, danger, plate, nozzle, nozzle_flow,
                            run, spools, hold)
    if reasons:
        raise SendError("; ".join(reasons))
    return result


def check(name: str, state, model_id: str, file_model_id: str, slots: list[tuple[str, str, str]],
          danger: list[str], plate: int = 1, nozzle: str = "", nozzle_flow: str = "",
          run: list[dict[str, float]] | None = None, spools: list[Spool] | None = None,
          hold: bool = True) -> tuple[Plan, list[str]]:
    """The plan and every reason it can't be sent yet (none: ready). Shown before you press Send.

    ``nozzle`` and ``nozzle_flow`` are what the file was sliced for. A different nozzle size stops it;
    a different flow (Standard on a High Flow nozzle) is a note, as Bambu Studio warns and lets you go on.

    ``run`` is the grams per slot of each loop the file prints, and ``spools`` the inventory's: with them,
    each tray's supply is checked against the whole run. A tray that runs out part-way is a note; one known
    to be short for the first loop stops the send while ``hold`` (filament settings: hold when short) is on.
    """
    reasons, notes = [], []
    fitted = float(getattr(state, "nozzle_diameter", 0.0) or 0.0)
    try:
        want = float(str(nozzle).split(",")[0] or 0)
    except ValueError:
        want = 0.0
    if fitted and want and abs(fitted - want) > 1e-3:
        reasons.append(f"the file was sliced for a {want:g} mm nozzle, {name} has a {fitted:g} mm one: "
                       f"drop its project or model on Make a loop file to slice it for {name}")
    has_flow = getattr(state, "nozzle_flow", "")
    if has_flow and nozzle_flow and has_flow != nozzle_flow:
        notes.append(f"sliced for a {nozzle_flow} nozzle, but {name} has a {has_flow} one: drop its project or "
                     f"model on Make a loop file to slice it for {name}")
    if not state.online:
        reasons.append(f"{name} isn't connected")
    elif not getattr(state, "reports", 1) and not state.gcode_state:
        reasons.append(f"{name} hasn't reported its state yet: check its serial number")
    elif state.gcode_state.upper() in BUSY:
        reasons.append(f"{name} is busy ({state.gcode_state.lower()})")
    if getattr(state, "commands_rejected", False):
        reasons.append(f"{name} rejects commands from ProofLoop: turn on Developer Mode (LAN-only mode) on it")
    if file_model_id and model_id != file_model_id:
        reasons.append(f"the file was sliced for another printer model ({file_model_id}, {name} is {model_id})")
    reasons += [f"the loop-file checks found: {d}" for d in danger]
    trays = state.ams.trays()
    if any(not t.get("empty") for t in trays):
        mapping, problems = match_trays(slots, trays)
        reasons += problems
        result = Plan(name, "", plate, True, mapping, notes)
    elif len(slots) <= 1:
        used = {int(s[0]) for s in slots if str(s[0]).isdigit()}
        spool = [EXTERNAL_SPOOL if i in used else -1 for i in range(1, max(used, default=0) + 1)]
        result = Plan(name, "", plate, False, spool, ["no AMS: printed from the external spool", *notes])
    else:
        result = Plan(name, "", plate, False, [], notes)
        reasons.append(f"{name} has no AMS, and the file uses {len(slots)} filaments")
    if run:
        fed = {str(i + 1): t for i, t in enumerate(result.ams_mapping) if t >= 0}
        result.filament = forecast(by_tray(run, fed), trays, spools or [], name)
        for r in result.filament:
            if r["stops"] and hold:
                reasons.append(r["text"])
            elif r["status"] in WARN:
                result.notes.append(r["text"])
    return result, reasons


def printer_filename(name: str) -> str:
    """``name`` as the printer takes it: letters, digits, ``. _ -`` only, always ending in ``.gcode.3mf``."""
    base = PurePosixPath(name.replace("\\", "/")).name
    stem = re.sub(r"(\.gcode)?\.3mf$", "", base, flags=re.IGNORECASE)
    stem = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9._-]", "_", stem)).strip("._") or "plate"
    return f"{stem}.gcode.3mf"


def remote_path(model_id: str, filename: str) -> str:
    """Where on the printer's storage the file goes, as FTP sees it."""
    return f"cache/{filename}" if model_id in CACHE_MODELS else filename


def plate_facts(data: bytes, plate: int) -> tuple[str, int]:
    """The MD5 of the plate's G-code and how many filaments the project lists (0: not said)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            gcode = z.read(f"Metadata/plate_{plate}.gcode")
    except (zipfile.BadZipFile, KeyError, OSError):
        return "", 0
    m = re.search(rb"^; filament_colour = (.*)$", gcode, re.MULTILINE)
    count = len([c for c in m.group(1).split(b";") if c.strip()]) if m else 0
    return hashlib.md5(gcode).hexdigest(), count


def _mapping2(tray: int) -> dict:
    if tray < 0:
        return {"ams_id": 255, "slot_id": 255}
    if tray >= EXTERNAL_SPOOL:
        return {"ams_id": tray, "slot_id": tray}
    if tray >= 128:  # AMS HT: one tray each
        return {"ams_id": tray, "slot_id": 0}
    return {"ams_id": tray // 4, "slot_id": tray % 4}


def start_command(p: Plan, model_id: str = "", md5: str = "", filaments: int = 0) -> dict:
    """The MQTT ``project_file`` command that prints a .gcode.3mf already on the printer."""
    stem = p.filename[: -len(".gcode.3mf")] if p.filename.lower().endswith(".gcode.3mf") else PurePosixPath(p.filename).stem
    param = f"Metadata/plate_{p.plate}.gcode"
    if model_id in CACHE_MODELS:
        mapping = p.ams_mapping + [-1] * (filaments - len(p.ams_mapping))
        task = str(int(time.time() * 1000) & 0x7FFFFFFF)  # the printer wants these non-zero
        return {"print": {
            "sequence_id": "0", "command": "project_file", "param": param,
            "url": f"ftp:///{remote_path(model_id, p.filename)}", "file": p.filename, "md5": md5,
            "bed_type": "auto", "timelapse": 0, "bed_leveling": 1, "auto_bed_leveling": 1, "flow_cali": 0,
            "vibration_cali": 0, "layer_inspect": 0, "use_ams": p.use_ams, "cfg": "0",
            "extrude_cali_flag": 0, "extrude_cali_manual_mode": 0, "nozzle_offset_cali": 2,
            "subtask_name": stem, "profile_id": "0", "project_id": task, "subtask_id": task, "task_id": task,
            "ams_mapping": mapping, "ams_mapping2": [_mapping2(t) for t in mapping],
        }}
    return {"print": {
        "sequence_id": "0", "command": "project_file", "param": param,
        "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0", "subtask_name": stem,
        "file": p.filename, "url": f"file:///sdcard/{p.filename}", "md5": md5,
        "timelapse": False, "bed_type": "auto", "bed_leveling": True, "flow_cali": False,
        "vibration_cali": False, "layer_inspect": False,
        "use_ams": p.use_ams, "ams_mapping": p.ams_mapping if p.use_ams else [],
    }}


class _ImplicitFTPS(ftplib.FTP_TLS):
    """Bambu printers speak FTPS with TLS from the first byte (port 990) and expect the data
    connection to reuse the control connection's TLS session."""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        self.host, self.port = host, port  # the data connection's TLS needs the host too
        sock = socket.create_connection((host, port), timeout if timeout != -999 else 30)
        self.af = sock.family
        self.sock = self.context.wrap_socket(sock, server_hostname=host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def upload(host: str, access_code: str, filename: str, data: bytes, timeout: float = 120, port: int = 990) -> None:
    """Put ``data`` on the printer's storage as ``filename`` (a path such as ``cache/x.gcode.3mf``)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # self-signed; the LAN access code is the credential
    ftp = _ImplicitFTPS(context=ctx, timeout=timeout)
    try:
        ftp.connect(host, port, timeout)
        ftp.login("bblp", access_code)
        ftp.prot_p()
        folder = PurePosixPath(filename).parent
        if str(folder) != ".":
            try:
                ftp.mkd(str(folder))
            except ftplib.error_perm:
                pass  # already there
        ftp.storbinary(f"STOR {filename}", io.BytesIO(data))
    except (OSError, ftplib.Error) as e:
        raise SendError(f"couldn't copy the file to the printer: {e}") from e
    finally:
        if ftp.sock is not None:  # never connected: nothing to close
            try:
                ftp.quit()
            except (OSError, ftplib.Error):
                ftp.close()
