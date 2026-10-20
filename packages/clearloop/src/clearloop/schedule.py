"""Quiet hours: run the ejector gently, or hold new jobs, overnight."""

from __future__ import annotations

from datetime import datetime, time


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_quiet_hours(quiet, now: datetime | None = None) -> bool:
    if quiet is None:
        return False
    now_t = (now or datetime.now()).time()
    start, end = _t(quiet.start), _t(quiet.end)
    if start <= end:
        return start <= now_t < end
    return now_t >= start or now_t < end  # window crosses midnight
