"""Built-in printer-family and material profiles.

Every value here is a starting point, not gospel. Anything can be overridden per
printer in the config file, and values marked ``verify`` should be checked on
your own machine before running unattended.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class PrinterProfile:
    model: str
    family: str  # "A", "P", "X", "H"
    bed_x: float
    bed_y: float
    max_z: float
    enclosed: bool
    heated_chamber: bool
    camera: str  # "tcp_jpeg" (A1/P1 chamber cam), "rtsps", or "none"
    # Z at which the plate is presented to the bender. On P/X/H the bed drops to
    # the bottom; on A-series the gantry rises to the top.
    bend_z: float
    # Coordinate the nozzle pushes parts to. Parts leave over the front edge.
    eject_y: float = -2.0
    # Lowest Z the nozzle may sweep at without touching the plate surface.
    min_sweep_z: float = 1.5
    travel_feed: int = 12000
    push_feed: int = 3000
    z_feed: int = 900
    notes: str = ""
    extra: dict = field(default_factory=dict)


_PROFILES: dict[str, PrinterProfile] = {}


def _add(p: PrinterProfile) -> None:
    _PROFILES[p.model] = p


_add(PrinterProfile("a1mini", "A", 180, 180, 180, False, False, "tcp_jpeg", bend_z=175))
_add(PrinterProfile("a1", "A", 256, 256, 256, False, False, "tcp_jpeg", bend_z=250))
_add(
    PrinterProfile(
        "p1p",
        "P",
        256,
        256,
        256,
        False,
        False,
        "none",
        bend_z=250,
        notes="No camera by default; bed verification needs an external snapshot URL.",
    )
)
_add(PrinterProfile("p1s", "P", 256, 256, 256, True, False, "tcp_jpeg", bend_z=250))
_add(PrinterProfile("p2s", "P", 256, 256, 256, True, False, "rtsps", bend_z=250, notes="verify"))
_add(PrinterProfile("x1c", "X", 256, 256, 256, True, False, "rtsps", bend_z=250))
_add(PrinterProfile("x1e", "X", 256, 256, 256, True, True, "rtsps", bend_z=250))
_add(PrinterProfile("x2d", "X", 256, 256, 256, True, True, "rtsps", bend_z=250, notes="verify"))
_add(PrinterProfile("h2s", "H", 340, 320, 340, True, True, "rtsps", bend_z=335, notes="verify"))
_add(PrinterProfile("h2d", "H", 325, 320, 325, True, True, "rtsps", bend_z=320, notes="verify"))
_add(PrinterProfile("h2c", "H", 325, 320, 325, True, True, "rtsps", bend_z=320, notes="verify"))


def printer_profile(model: str, **overrides) -> PrinterProfile:
    key = model.lower().replace(" ", "").replace("-", "")
    if key not in _PROFILES:
        raise KeyError(f"unknown printer model {model!r}; known: {', '.join(sorted(_PROFILES))}")
    base = _PROFILES[key]
    clean = {k: v for k, v in overrides.items() if v is not None}
    return replace(base, **clean) if clean else base


def known_models() -> list[str]:
    return sorted(_PROFILES)


@dataclass(frozen=True)
class MaterialProfile:
    name: str
    release_temp: float  # bed temperature (C) at which parts release from textured PEI
    natural_cooldown_min: int  # minutes with door shut and fans off before opening up
    supported: bool = True
    reason: str = ""


_MATERIALS: dict[str, MaterialProfile] = {
    "PLA": MaterialProfile("PLA", 30, 0),
    "PLA-CF": MaterialProfile("PLA-CF", 30, 0),
    "PETG": MaterialProfile("PETG", 30, 0),
    "PETG-CF": MaterialProfile("PETG-CF", 30, 0),
    "PCTG": MaterialProfile("PCTG", 32, 0),
    "ABS": MaterialProfile("ABS", 40, 20),
    "ASA": MaterialProfile("ASA", 40, 20),
    "PC": MaterialProfile("PC", 45, 30),
    "PA": MaterialProfile("PA", 40, 30),
    "PA-CF": MaterialProfile("PA-CF", 40, 30),
    "PAHT-CF": MaterialProfile("PAHT-CF", 40, 30),
    "TPU": MaterialProfile(
        "TPU", 30, 0, supported=False, reason="flexible parts deform instead of releasing"
    ),
    "PVA": MaterialProfile(
        "PVA", 30, 0, supported=False, reason="soluble support needs manual post-processing"
    ),
}

# Materials that warp when the door opens and fans hit hot parts.
WARP_PRONE = {"ABS", "ASA", "PC", "PA", "PA-CF", "PAHT-CF"}


def material_profile(name: str) -> MaterialProfile:
    key = name.upper().strip()
    if key in _MATERIALS:
        return _MATERIALS[key]
    # "PLA Basic", "PETG HF", "ABS-GF" and friends map onto their base polymer.
    for base in sorted(_MATERIALS, key=len, reverse=True):
        if key.startswith(base):
            return _MATERIALS[base]
    return MaterialProfile(key, 30, 0, supported=True, reason="unknown material, using PLA defaults")


def combine_materials(names: list[str]) -> MaterialProfile:
    """The strictest settings across every filament used on a plate."""
    if not names:
        return _MATERIALS["PLA"]
    profiles = [material_profile(n) for n in names]
    unsupported = [p for p in profiles if not p.supported]
    if unsupported:
        p = unsupported[0]
        return MaterialProfile(p.name, p.release_temp, p.natural_cooldown_min, False, p.reason)
    return MaterialProfile(
        "+".join(sorted({p.name for p in profiles})),
        # Cool to the lowest release temperature so every material lets go.
        release_temp=min(p.release_temp for p in profiles),
        natural_cooldown_min=max(p.natural_cooldown_min for p in profiles),
    )
