"""Automated accessibility check from the screenshots of a run (pixels only, approximate).

Measured per screen the personas saw:

* text contrast against WCAG 2.2 AA (4.5:1 for normal text, 3:1 for large text);
* text that is very small (roughly under 12 px);
* tap targets smaller than 24 x 24 CSS px on touch devices (WCAG 2.2 target size, AA).

These are measurements, not persona opinions, and they are reported separately
from the persona findings. Text sizes come from OCR boxes, so they are estimates;
contrast is measured between the text pixels and their background on the
screenshot (anti-aliasing and JPEG compression lower it slightly).
"""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from proofloop.core.config import Settings
from proofloop.core.db import Database
from proofloop.web.runner.perception import Screen, TextBox, contrast_ratio, hash_distance, perceive

LARGE_TEXT_PX = 30        # OCR box height of ~24 px text (18 pt), which WCAG counts as large
SMALL_TEXT_PX = 13        # OCR box height of ~11 px text
MIN_TARGET_CSS_PX = 24    # WCAG 2.2 SC 2.5.8 (AA)
JPEG_ALLOWANCE = 0.25     # screenshots are JPEG; don't flag contrast within this margin of the threshold


@dataclass
class A11yFinding:
    check: str            # contrast | small_text | tap_target
    screen_id: str
    text: str
    measured: float
    required: float
    severity: int         # 1 cosmetic .. 3 major
    region: dict[str, int]
    screenshot: str
    devices: list[str]

    @property
    def title(self) -> str:
        return {"contrast": "Text contrast below WCAG AA", "small_text": "Very small text",
                "tap_target": "Tap target smaller than 24 x 24 px"}[self.check]

    @property
    def detail(self) -> str:
        if self.check == "contrast":
            return (f"'{self.text}' has a contrast of about {self.measured:.1f}:1; "
                    f"WCAG AA asks for {self.required:g}:1 at this size.")
        if self.check == "small_text":
            return f"'{self.text}' is about {self.measured:.0f} px tall on screen; under ~12 px is hard to read."
        return (f"'{self.text}' is a target about {self.measured:.0f} px tall on a touch screen; "
                f"WCAG AA asks for at least {self.required:g} px.")


def ink_contrast(screen: Screen, t: TextBox) -> float:
    """Contrast between the solid core of the letters and their background.

    The edges of letters are anti-aliased (blended with the background); WCAG is about
    the text colour itself, so take the pixels that differ most from the background.
    """
    if screen.lum is None:
        return t.contrast
    lum = screen.lum
    h, w = lum.shape
    crop = lum[max(0, t.y0 - 2):min(h, t.y1 + 2), max(0, t.x0 - 2):min(w, t.x1 + 2)]
    if crop.size < 20:
        return t.contrast
    border = np.concatenate([crop[0, :], crop[-1, :], crop[:, 0], crop[:, -1]])
    bg = float(np.median(border))
    diff = np.abs(crop - bg)
    core = crop[diff >= np.quantile(diff, 0.97)]
    fg = float(np.median(core)) if core.size else bg
    return round(contrast_ratio(fg, bg), 2)


def check_screen(screen: Screen, *, screen_id: str, screenshot: str, device: str, touch: bool,
                 scale: float = 1.0) -> list[A11yFinding]:
    out: list[A11yFinding] = []
    for t in screen.texts:
        if len(t.norm) < 2 or t.conf < 0.6:
            continue
        large = t.height >= LARGE_TEXT_PX * scale
        need = 3.0 if large else 4.5
        contrast = ink_contrast(screen, t)
        if contrast + JPEG_ALLOWANCE < need:
            out.append(A11yFinding("contrast", screen_id, t.text, contrast, need, 3 if contrast < 3.0 else 2,
                                   t.region(6), screenshot, [device]))
        if t.height < SMALL_TEXT_PX * scale:
            out.append(A11yFinding("small_text", screen_id, t.text, t.height / scale, 12, 2, t.region(6),
                                   screenshot, [device]))
        if touch and _looks_tappable(t):
            target = _target_height(t, screen) / scale   # the button around the label, not the label itself
            if target < MIN_TARGET_CSS_PX:
                out.append(A11yFinding("tap_target", screen_id, t.text, target, MIN_TARGET_CSS_PX, 2, t.region(6),
                                       screenshot, [device]))
    return out


def _looks_tappable(t: TextBox) -> bool:
    return t.on_fill and len(t.norm.split()) <= 4


def _target_height(t: TextBox, screen: Screen) -> float:
    """Height of the filled button around the text (its background colour), not just the text."""
    if screen.lum is None:
        return float(t.height)
    lum = screen.lum
    cx = min(max(t.center[0], 0), lum.shape[1] - 1)
    bg = t.bg_lum
    top, bottom = t.y0, t.y1
    while top > 0 and abs(float(lum[top - 1, cx]) - bg) < 0.06 and t.y0 - top < 60:
        top -= 1
    while bottom < lum.shape[0] - 1 and abs(float(lum[bottom + 1, cx]) - bg) < 0.06 and bottom - t.y1 < 60:
        bottom += 1
    return float(bottom - top + 1)


def audit_run(settings: Settings, db: Database, run_id: str, max_screens: int = 60) -> list[A11yFinding]:
    """Check each distinct screen of a run once (per device); merge the same finding across devices."""
    rows = db.all("SELECT st.screen_id, st.screenshot_path, st.screen_hash, ses.conditions_json "
                  "FROM steps st JOIN sessions ses ON ses.id = st.session_id WHERE ses.run_id = ? "
                  "ORDER BY st.screen_id, ses.id, st.idx", [run_id])
    seen: list[tuple[str, str, int]] = []
    merged: dict[tuple[str, str, str], A11yFinding] = {}
    for row in rows:
        shot = row["screenshot_path"]
        if not shot or not Path(shot).is_file():
            continue
        device = (row["conditions_json"] or {}).get("device") or "desktop"
        h = int(row["screen_hash"] or 0)
        if any(sid == row["screen_id"] and dev == device and hash_distance(h, other) <= 6
               for sid, dev, other in seen):
            continue
        if len(seen) >= max_screens:
            break
        seen.append((row["screen_id"], device, h))
        cfg = settings.devices.get(device)
        buf = io.BytesIO()
        Image.open(shot).convert("RGB").save(buf, "PNG")
        found = check_screen(perceive(buf.getvalue()), screen_id=row["screen_id"], screenshot=shot, device=device,
                             touch=bool(cfg and cfg.mobile), scale=float(cfg.scale) if cfg else 1.0)
        for f in found:
            key = (f.check, f.screen_id, f.text.lower())
            if key in merged:
                if device not in merged[key].devices:
                    merged[key].devices.append(device)
                merged[key].measured = min(merged[key].measured, f.measured)
            else:
                merged[key] = f
    return sorted(merged.values(), key=lambda f: (-f.severity, f.check, f.screen_id, f.measured))


def as_rows(findings: list[A11yFinding]) -> list[dict[str, Any]]:
    return [{**asdict(f), "title": f.title, "detail": f.detail} for f in findings]
