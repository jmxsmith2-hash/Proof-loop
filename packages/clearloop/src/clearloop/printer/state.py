"""Printer status as reported over Bambu's local MQTT ``device/<serial>/report`` topic.

P1 and A1 printers send partial updates, so reports are merged into one state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

HMS_SEVERITY = {1: "fatal", 2: "serious", 3: "common", 4: "info"}


@dataclass(frozen=True)
class HmsEvent:
    attr: int
    code: int

    @property
    def code_str(self) -> str:
        return (
            f"{self.attr >> 16:04X}_{self.attr & 0xFFFF:04X}_{self.code >> 16:04X}_{self.code & 0xFFFF:04X}"
        )

    @property
    def severity(self) -> str:
        return HMS_SEVERITY.get(self.code >> 16, "unknown")

    @property
    def blocking(self) -> bool:
        return self.severity in ("fatal", "serious")

    @property
    def wiki_url(self) -> str:
        return f"https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/{self.code_str}"


@dataclass
class PrinterState:
    gcode_state: str = "UNKNOWN"  # IDLE, PREPARE, RUNNING, PAUSE, FINISH, FAILED
    percent: int = 0
    remaining_min: int = 0
    layer_num: int | None = None
    total_layers: int = 0
    gcode_file: str = ""
    subtask_name: str = ""
    print_error: int = 0
    bed_temp: float = 0.0
    nozzle_temp: float = 0.0
    chamber_temp: float = 0.0
    hms: list[HmsEvent] = field(default_factory=list)
    ams_types: list[str] = field(default_factory=list)  # filament types loaded in the AMS / external spool
    firmware: str = ""
    online: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def blocking_errors(self) -> list[str]:
        out = [f"HMS {h.code_str} ({h.severity})" for h in self.hms if h.blocking]
        if self.print_error:
            out.append(f"print_error 0x{self.print_error:08X}")
        return out

    def merge(self, report: dict) -> None:
        """Apply one MQTT report payload."""
        p = report.get("print")
        if isinstance(p, dict):
            self.raw.update(p)
            self.gcode_state = str(p.get("gcode_state", self.gcode_state))
            self.percent = int(p.get("mc_percent", self.percent) or 0)
            self.remaining_min = int(p.get("mc_remaining_time", self.remaining_min) or 0)
            if "layer_num" in p:
                self.layer_num = int(p["layer_num"])
            self.total_layers = int(p.get("total_layer_num", self.total_layers) or 0)
            self.gcode_file = str(p.get("gcode_file", self.gcode_file))
            self.subtask_name = str(p.get("subtask_name", self.subtask_name))
            if "print_error" in p:
                self.print_error = int(p["print_error"] or 0)
            self.bed_temp = float(p.get("bed_temper", self.bed_temp) or 0)
            self.nozzle_temp = float(p.get("nozzle_temper", self.nozzle_temp) or 0)
            self.chamber_temp = float(p.get("chamber_temper", self.chamber_temp) or 0)
            if "hms" in p and isinstance(p["hms"], list):
                self.hms = [HmsEvent(int(h.get("attr", 0)), int(h.get("code", 0))) for h in p["hms"]]
            if "ams" in p or "vt_tray" in p:
                # Partial reports often carry an AMS section without tray data.
                self.ams_types = _filament_types(p) or self.ams_types
        info = report.get("info")
        if isinstance(info, dict) and isinstance(info.get("module"), list):
            for m in info["module"]:
                if m.get("name") == "ota" and m.get("sw_ver"):
                    self.firmware = str(m["sw_ver"])

    def as_dict(self) -> dict:
        return {
            "gcode_state": self.gcode_state,
            "percent": self.percent,
            "remaining_min": self.remaining_min,
            "layer_num": self.layer_num,
            "total_layers": self.total_layers,
            "gcode_file": self.gcode_file,
            "bed_temp": self.bed_temp,
            "nozzle_temp": self.nozzle_temp,
            "chamber_temp": self.chamber_temp,
            "hms": [{"code": h.code_str, "severity": h.severity, "url": h.wiki_url} for h in self.hms],
            "print_error": self.print_error,
            "ams_types": self.ams_types,
            "firmware": self.firmware,
            "online": self.online,
        }


def _filament_types(p: dict) -> list[str]:
    types: list[str] = []
    ams = p.get("ams")
    if isinstance(ams, dict):
        for unit in ams.get("ams", []) or []:
            for tray in unit.get("tray", []) or []:
                if tray.get("tray_type"):
                    types.append(str(tray["tray_type"]))
    vt = p.get("vt_tray")
    if isinstance(vt, dict) and vt.get("tray_type"):
        types.append(str(vt["tray_type"]))
    return sorted(set(types))
