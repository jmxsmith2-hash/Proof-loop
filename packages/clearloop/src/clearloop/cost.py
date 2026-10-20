"""Cost per part: filament, energy, machine time and plate wear."""

from __future__ import annotations


def cost_per_part(
    grams: float, print_seconds: float, cycle_seconds: float, parts: int, costs, rated_cycles: int = 1500
) -> dict:
    parts = max(1, parts)
    hours = max(cycle_seconds, print_seconds) / 3600
    filament = grams / 1000 * costs.filament_per_kg
    energy = hours * costs.power_kw * costs.kwh_price
    machine = hours * costs.machine_per_hour
    plate = costs.plate_cost / max(1, rated_cycles)
    total = filament + energy + machine + plate
    return {
        "currency": costs.currency,
        "per_plate": round(total, 4),
        "per_part": round(total / parts, 4),
        "breakdown": {
            "filament": round(filament, 4),
            "energy": round(energy, 4),
            "machine": round(machine, 4),
            "plate_wear": round(plate, 4),
        },
    }
