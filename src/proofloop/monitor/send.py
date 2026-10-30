"""Send a loop file to a printer and start it, when you've turned that on and confirm each time.

Off by default (``send_to_printer`` in settings.json). Before anything is sent, ProofLoop checks:

* the printer is connected, the same model the file was sliced for, and not busy (idle, finished
  or failed; never printing, preparing or paused);
* the loop-file checks find nothing dangerous in the file;
* every filament slot the file uses has a matching spool loaded (same type, nearest colour), so
  the printer is told which AMS tray feeds which slot;
* you confirmed this printer for this file (the request names the printer twice).

The file goes over the printer's own FTPS (implicit TLS on port 990, user ``bblp``, the LAN access
code) to its storage, then ``project_file`` over MQTT starts it. Both follow Bambuddy and the
Bambu Studio LAN protocol (see docs/sources.md); this path has not been tried on every model, so
the first send to a new printer is worth watching.
"""

from __future__ import annotations

import ftplib
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import PurePosixPath

BUSY = ("RUNNING", "PREPARE", "PAUSE", "SLICING")


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


def plan(name: str, state, model_id: str, file_model_id: str, slots: list[tuple[str, str, str]],
         danger: list[str], plate: int = 1) -> Plan:
    """Everything that must be true before a file goes to ``name``. Raises SendError with all reasons."""
    reasons = []
    if not state.online:
        reasons.append(f"{name} isn't connected")
    elif state.gcode_state.upper() in BUSY:
        reasons.append(f"{name} is busy ({state.gcode_state.lower()})")
    if file_model_id and model_id != file_model_id:
        reasons.append(f"the file was sliced for another printer model ({file_model_id}, {name} is {model_id})")
    reasons += [f"the loop-file checks found: {d}" for d in danger]
    trays = state.ams.trays()
    if any(not t.get("empty") for t in trays):
        mapping, problems = match_trays(slots, trays)
        reasons += problems
        result = Plan(name, "", plate, True, mapping)
    elif len(slots) <= 1:
        result = Plan(name, "", plate, False, [], ["no AMS: printed from the external spool"])
    else:
        result = Plan(name, "", plate, False)
        reasons.append(f"{name} has no AMS, and the file uses {len(slots)} filaments")
    if reasons:
        raise SendError("; ".join(reasons))
    return result


def start_command(p: Plan) -> dict:
    """The MQTT ``project_file`` command that prints a .gcode.3mf already on the printer."""
    stem = p.filename[: -len(".gcode.3mf")] if p.filename.lower().endswith(".gcode.3mf") else PurePosixPath(p.filename).stem
    return {"print": {
        "sequence_id": "0", "command": "project_file", "param": f"Metadata/plate_{p.plate}.gcode",
        "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0", "subtask_name": stem,
        "file": p.filename, "url": f"file:///sdcard/{p.filename}", "md5": "",
        "timelapse": False, "bed_type": "auto", "bed_levelling": True, "flow_cali": False,
        "vibration_cali": False, "layer_inspect": False,
        "use_ams": p.use_ams, "ams_mapping": p.ams_mapping,
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
    """Put ``data`` on the printer's storage as ``filename``."""
    import io

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # self-signed; the LAN access code is the credential
    ftp = _ImplicitFTPS(context=ctx, timeout=timeout)
    try:
        ftp.connect(host, port, timeout)
        ftp.login("bblp", access_code)
        ftp.prot_p()
        ftp.storbinary(f"STOR {filename}", io.BytesIO(data))
    except (OSError, ftplib.Error) as e:
        raise SendError(f"couldn't copy the file to the printer: {e}") from e
    finally:
        if ftp.sock is not None:  # never connected: nothing to close
            try:
                ftp.quit()
            except (OSError, ftplib.Error):
                ftp.close()
