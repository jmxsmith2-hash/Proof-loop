"""Hardware profiles: the printer- and kit-specific moves of a FarmLoop setup.

A profile holds everything that depends on the physical FarmLoop hardware and
that ProofLoop must therefore never make up: the FarmBoard trigger gestures,
bend timing, the sweep path the pusher was designed for, where to park. Every
profile records where its values came from.

* Built-in profiles ship with ProofLoop and cite their public source.
* Imported profiles are learned from your own files: a plain slice plus the
  FarmLoop app's processed version of the same plate (see importer.py). That is
  how Stage 1 kits for any printer, and other Stage 2 kits, are supported
  without guessing bend positions.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

BUILTIN_DIR = Path(__file__).parent / "profile_data"


@dataclass
class SweepPass:
    feed: int
    lanes: list[float]  # X positions, in the order the vendor sweeps them


@dataclass
class SweepGeometry:
    y_rear: float  # pushes start here (behind the parts)
    y_front: float  # ...and end here (parts leave over the front edge)
    passes: list[SweepPass]
    z_feed: int  # feed used for Z moves inside the sweep
    park: list[str] = field(default_factory=list)  # verbatim moves after the sweep

    @property
    def lane_pitch(self) -> float:
        """The vendor's step between neighbouring rake lanes (the most common step in its passes)."""
        steps: dict[float, int] = {}
        for p in self.passes:
            for a, b in zip(p.lanes, p.lanes[1:], strict=False):
                d = round(abs(b - a), 1)
                if d > 0.5:
                    steps[d] = steps.get(d, 0) + 1
        if not steps:
            raise ValueError("sweep has a single lane; lane spacing unknown")
        return max(steps, key=lambda d: (steps[d], -d))

    @property
    def lanes(self) -> list[float]:
        return sorted({x for p in self.passes for x in p.lanes})


@dataclass
class HardwareProfile:
    id: str
    name: str
    printer_model_id: str
    stage: int  # 1 = no electronics, 2 = FarmBoard
    description: str
    source: dict
    sweep: SweepGeometry
    start_anchor: str | None = None  # regex; the start block goes right after the first matching line
    start_block: str = ""  # verbatim
    door_open: str = ""  # verbatim FarmBoard step (sentinel + switch gesture), Stage 2
    bend: str = ""  # verbatim bend step: FarmBoard trigger + wait, or a Stage 1 static bend
    safety: str = ""  # verbatim FarmBoard safety check
    door_close: str = ""  # verbatim FarmBoard final door close
    cooldown_fan: list[str] = field(default_factory=list)  # verbatim fan-on lines used while cooling
    cooldown_repeat: int = 1  # how many times the vendor repeats the bed-temperature wait
    fans_off: list[str] = field(default_factory=list)  # verbatim fan-off lines at the end
    notes: list[str] = field(default_factory=list)

    @property
    def has_bender_steps(self) -> bool:
        return bool(self.bend.strip())

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> HardwareProfile:
        d = dict(d)
        sw = d.pop("sweep")
        sweep = SweepGeometry(
            y_rear=sw["y_rear"],
            y_front=sw["y_front"],
            passes=[SweepPass(p["feed"], list(p["lanes"])) for p in sw["passes"]],
            z_feed=sw["z_feed"],
            park=list(sw.get("park", [])),
        )
        return cls(sweep=sweep, **d)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class ProfileStore:
    """Built-in profiles plus the ones a user imported (stored as JSON in their data folder)."""

    def __init__(self, user_dir: str | Path | None = None):
        self.user_dir = Path(user_dir) if user_dir else None

    def _files(self) -> list[tuple[Path, bool]]:
        out = [(p, True) for p in sorted(BUILTIN_DIR.glob("*.json"))]
        if self.user_dir and self.user_dir.exists():
            out += [(p, False) for p in sorted(self.user_dir.glob("*.json"))]
        return out

    def all(self) -> list[HardwareProfile]:
        profiles = []
        for path, _builtin in self._files():
            profiles.append(HardwareProfile.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return profiles

    def get(self, profile_id: str) -> HardwareProfile:
        for p in self.all():
            if p.id == profile_id:
                return p
        raise KeyError(f"no hardware profile {profile_id!r}")

    def for_printer(self, model_id: str) -> list[HardwareProfile]:
        return [p for p in self.all() if p.printer_model_id == model_id]

    def save(self, profile: HardwareProfile) -> Path:
        if not self.user_dir:
            raise RuntimeError("no folder configured for imported profiles")
        if any(p.id == profile.id for p in self.all() if p.source.get("kind") == "builtin"):
            raise ValueError(f"{profile.id!r} is a built-in profile name; choose another")
        self.user_dir.mkdir(parents=True, exist_ok=True)
        path = self.user_dir / f"{profile.id}.json"
        path.write_text(profile.to_json(), encoding="utf-8")
        return path

    def delete(self, profile_id: str) -> None:
        if not self.user_dir:
            raise KeyError(profile_id)
        path = self.user_dir / f"{profile_id}.json"
        if not path.exists():
            raise KeyError(f"{profile_id!r} is not an imported profile")
        path.unlink()
