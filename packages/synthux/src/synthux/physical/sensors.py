"""Prototype sensor / usage data: anomalies and usage patterns (numpy only)."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

TIME_COLUMNS = ("timestamp", "time", "datetime", "date", "ts")


@dataclass
class SensorSeries:
    name: str
    times: np.ndarray           # seconds since epoch
    values: np.ndarray


@dataclass
class SensorReport:
    source: str
    findings: list[dict[str, Any]] = field(default_factory=list)


def _parse_time(value: str) -> float:
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def load_sensor_csv(path: Path) -> list[SensorSeries]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return []
    time_col = next((c for c in rows[0] if c.strip().lower() in TIME_COLUMNS), None)
    if time_col is None:
        raise ValueError(f"{path.name}: needs a timestamp column ({', '.join(TIME_COLUMNS)})")
    times = np.array([_parse_time(r[time_col]) for r in rows])
    series = []
    for col in rows[0]:
        if col == time_col:
            continue
        try:
            vals = np.array([float(r[col]) if r[col] not in ("", None) else np.nan for r in rows])
        except ValueError:
            continue
        series.append(SensorSeries(col, times, vals))
    return series


def rolling_zscore_anomalies(values: np.ndarray, window: int = 30, z: float = 4.0) -> list[int]:
    out = []
    for i in range(len(values)):
        lo = max(0, i - window)
        ref = values[lo:i]
        ref = ref[~np.isnan(ref)]
        if len(ref) < max(8, window // 3) or np.isnan(values[i]):
            continue
        std = ref.std()
        if std > 0 and abs(values[i] - ref.mean()) / std > z:
            out.append(i)
    return out


def iqr_outliers(values: np.ndarray, k: float = 3.0) -> list[int]:
    clean = values[~np.isnan(values)]
    if clean.size < 8:
        return []
    q1, q3 = np.percentile(clean, [25, 75])
    spread = q3 - q1
    if spread == 0:
        return []
    return [i for i, v in enumerate(values) if not np.isnan(v) and (v < q1 - k * spread or v > q3 + k * spread)]


def flat_lines(values: np.ndarray, min_run: int = 20) -> list[tuple[int, int]]:
    """Runs where the reading does not change at all (stuck sensor)."""
    runs, start = [], 0
    for i in range(1, len(values) + 1):
        if i == len(values) or values[i] != values[i - 1]:
            if i - start >= min_run:
                runs.append((start, i - 1))
            start = i
    return runs


def usage_sessions(times: np.ndarray, values: np.ndarray, threshold: float | None = None,
                   gap_s: float = 900) -> list[tuple[float, float]]:
    """Periods of activity (value above threshold), split by gaps longer than gap_s."""
    threshold = threshold if threshold is not None else float(np.nanmedian(values))
    active = times[values > threshold]
    if active.size == 0:
        return []
    sessions, start, prev = [], active[0], active[0]
    for t in active[1:]:
        if t - prev > gap_s:
            sessions.append((start, prev))
            start = t
        prev = t
    sessions.append((start, prev))
    return sessions


def analyse(path: Path, usage_column: str | None = None) -> SensorReport:
    report = SensorReport(path.name)
    for s in load_sensor_csv(path):
        z_hits = rolling_zscore_anomalies(s.values)
        iqr_hits = iqr_outliers(s.values)
        anomalies = sorted(set(z_hits) | set(iqr_hits))
        if anomalies:
            report.findings.append({
                "kind": "anomaly", "column": s.name, "count": len(anomalies),
                "examples": [{"time": datetime.fromtimestamp(s.times[i], timezone.utc).isoformat(timespec="minutes"),
                              "value": round(float(s.values[i]), 3)} for i in anomalies[:5]],
                "detail": f"{len(anomalies)} reading(s) far outside the usual range of {s.name}"})
        for start, end in flat_lines(s.values):
            report.findings.append({
                "kind": "flat_line", "column": s.name, "samples": end - start + 1,
                "from": datetime.fromtimestamp(s.times[start], timezone.utc).isoformat(timespec="minutes"),
                "detail": f"{s.name} stayed exactly at {s.values[start]:g} for {end - start + 1} samples "
                          "(sensor stuck or device off)"})
        if usage_column is None or s.name == usage_column:
            sess = usage_sessions(s.times, s.values)
            if sess:
                durations = np.array([(b - a) / 60 for a, b in sess])
                hours = np.array([datetime.fromtimestamp(a, timezone.utc).hour for a, _ in sess])
                busiest = int(np.bincount(hours, minlength=24).argmax())
                report.findings.append({
                    "kind": "usage", "column": s.name, "sessions": len(sess),
                    "median_minutes": round(float(np.median(durations)), 1),
                    "busiest_hour_utc": busiest,
                    "duty_cycle": round(float(np.mean(s.values > np.nanmedian(s.values))), 3),
                    "detail": f"{len(sess)} usage session(s) in {s.name}, median {np.median(durations):.0f} min, "
                              f"most often starting around {busiest:02d}:00 UTC"})
    return report
