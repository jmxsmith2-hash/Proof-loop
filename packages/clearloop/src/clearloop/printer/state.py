"""Printer status from Bambu's local MQTT ``device/<serial>/report`` topic.

P1/A1-class printers send partial updates, so reports are merged into one state.
Field names and HMS decoding follow Bambu Studio as documented in Bambuddy's
source (see docs/sources.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ams import AmsState, label, valid_tray

# HMS alert level, from the high 16 bits of ``code`` (Bambu Studio DevHMSItem::parse_hms_info).
HMS_LEVEL = {1: "error", 2: "warning", 3: "notification"}
# "MQTT command verification failed": the printer rejects control commands (Developer Mode off).
HMS_COMMAND_REJECTED = "0500050000010007"


@dataclass(frozen=True)
class HmsEvent:
    attr: int
    code: int

    @property
    def full(self) -> str:
        return f"{self.attr:08X}{self.code:08X}"

    @property
    def code_str(self) -> str:
        return (
            f"{self.attr >> 16:04X}-{self.attr & 0xFFFF:04X}-{self.code >> 16:04X}-{self.code & 0xFFFF:04X}"
        )

    @property
    def level(self) -> str:
        return HMS_LEVEL.get(self.code >> 16, "unknown")


@dataclass
class PrinterState:
    online: bool = False
    gcode_state: str = ""  # IDLE, PREPARE, RUNNING, PAUSE, FINISH, FAILED
    percent: int = 0
    remaining_min: int = 0
    layer_num: int = 0
    total_layers: int = 0
    nozzle_target: float = 0.0  # FarmBoard sentinels arrive here (M104 S1..S5)
    bed_target: float = 0.0
    print_error: int = 0  # Bambu's error code for the current job (0 = none)
    bed_temp: float = 0.0
    nozzle_temp: float = 0.0
    subtask_name: str = ""
    gcode_file: str = ""
    hms: list[HmsEvent] = field(default_factory=list)
    firmware: str = ""
    ams: AmsState = field(default_factory=AmsState)

    def merge(self, report: dict) -> None:
        p = report.get("print")
        if isinstance(p, dict):
            self.gcode_state = str(p.get("gcode_state", self.gcode_state))
            self.percent = _int(p.get("mc_percent"), self.percent)
            self.remaining_min = _int(p.get("mc_remaining_time"), self.remaining_min)
            self.layer_num = _int(p.get("layer_num"), self.layer_num)
            self.total_layers = _int(p.get("total_layer_num"), self.total_layers)
            self.nozzle_target = _float(p.get("nozzle_target_temper"), self.nozzle_target)
            self.bed_target = _float(p.get("bed_target_temper"), self.bed_target)
            self.print_error = _int(p.get("print_error"), self.print_error)
            self.bed_temp = _float(p.get("bed_temper"), self.bed_temp)
            self.nozzle_temp = _float(p.get("nozzle_temper"), self.nozzle_temp)
            self.subtask_name = str(p.get("subtask_name", self.subtask_name))
            self.gcode_file = str(p.get("gcode_file", self.gcode_file))
            self.ams.merge_print(p)
            if isinstance(p.get("hms"), list):
                events = []
                for h in p["hms"]:
                    if not isinstance(h, dict):
                        continue
                    attr, code = _hex_or_int(h.get("attr")), _hex_or_int(h.get("code"))
                    if code >= 0x4000:  # lower values are status indicators, not faults
                        events.append(HmsEvent(attr, code))
                self.hms = events
        info = report.get("info")
        if isinstance(info, dict) and isinstance(info.get("module"), list):
            for m in info["module"]:
                if isinstance(m, dict) and m.get("name") == "ota" and m.get("sw_ver"):
                    self.firmware = str(m["sw_ver"])

    @property
    def commands_rejected(self) -> bool:
        return any(h.full == HMS_COMMAND_REJECTED for h in self.hms)

    def as_dict(self) -> dict:
        return {
            "online": self.online,
            "gcode_state": self.gcode_state,
            "percent": self.percent,
            "remaining_min": self.remaining_min,
            "layer": self.layer_num,
            "total_layers": self.total_layers,
            "nozzle_target": self.nozzle_target,
            "bed_temp": self.bed_temp,
            "nozzle_temp": self.nozzle_temp,
            "job": self.subtask_name,
            "hms": [{"code": h.code_str, "level": h.level} for h in self.hms],
            "firmware": self.firmware,
            "ams": {
                "units": self.ams.units_dict(),
                "trays": self.ams.trays(),
                "tray_now": self.ams.tray_now if valid_tray(self.ams.tray_now) else None,
                "tray_now_label": label(self.ams.tray_now) if valid_tray(self.ams.tray_now) else None,
            },
        }


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _hex_or_int(v) -> int:
    if isinstance(v, str):
        try:
            return int(v.replace("0x", ""), 16) if v else 0
        except ValueError:
            return 0
    return _int(v, 0)
