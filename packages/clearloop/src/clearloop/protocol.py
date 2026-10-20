"""The ClearLoop trigger protocol (v1).

Processed G-code announces loop phases to the outside world through a value the
printer already reports over its local MQTT broker. By default the marker is
written as ``M73 L<base + code>``: the printer echoes it back as
``print.layer_num`` in its next status report, so the controller (or a
stand-alone ClearBoard) sees each phase without needing a switch press or a
special filename.

The protocol is documented in docs/trigger-protocol.md so any queue, firmware or
ejector can implement it. Run ``clearloop probe-marker`` once per printer and
firmware version to prove the channel works on your machine.
"""

from __future__ import annotations

from enum import IntEnum

PROTOCOL_VERSION = 1
DEFAULT_BASE = 9000
DEFAULT_HOLD_S = 2


class Marker(IntEnum):
    JOB_START = 1  # before homing: retract the bender, close the door
    DOOR_OPEN = 2  # warp-safe cooldown is over; door may open, fans come on
    EJECT = 3  # plate is at the bend position; bend now
    SWEEP_DONE = 4  # toolhead sweep finished; plate should be empty
    LOOP_END = 5  # a copy finished inside a multi-copy file


def encode(marker: Marker, base: int = DEFAULT_BASE, hold_s: int = DEFAULT_HOLD_S) -> str:
    """G-code that raises ``marker`` and holds it long enough to be reported."""
    lines = [
        f"; CLEARLOOP_MARKER {marker.name}",
        "M400",
        f"M73 L{base + int(marker)}",
    ]
    if hold_s > 0:
        lines.append(f"G4 S{hold_s}")
    return "\n".join(lines) + "\n"


def decode(layer_num: int | None, base: int = DEFAULT_BASE) -> Marker | None:
    if layer_num is None:
        return None
    code = int(layer_num) - base
    try:
        return Marker(code)
    except ValueError:
        return None


class MarkerEdgeDetector:
    """Turns a stream of reported layer numbers into marker events.

    A marker stays in the report until the next ``M73 L``, so only a change of
    value counts as a new event. Two identical markers in a row (consecutive
    loops in one file) are separated by real layer numbers in between.
    """

    def __init__(self, base: int = DEFAULT_BASE):
        self.base = base
        self._last: int | None = None

    def feed(self, layer_num: int | None) -> Marker | None:
        if layer_num is None or layer_num == self._last:
            return None
        self._last = layer_num
        return decode(layer_num, self.base)

    def reset(self) -> None:
        self._last = None
