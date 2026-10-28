import pytest
from PIL import Image, ImageDraw

from tests.fonts import font


@pytest.fixture
def shot(tmp_path):
    """A screenshot with a heading, a faint button and an error message."""
    img = Image.new("RGB", (900, 500), "white")
    d = ImageDraw.Draw(img)
    face = font(26)
    d.text((40, 40), "Review and pay", fill="black", font=face)
    d.text((40, 140), "Error 0x3F", fill=(140, 16, 16), font=face)
    d.text((600, 400), "Continue", fill=(200, 200, 200), font=face)
    path = tmp_path / "shot.jpg"
    img.save(path, "JPEG", quality=92)
    return str(path)


def make_issue(shot, **kw):
    base = {"id": "iss_1", "session_id": "ses_1", "persona_id": "per_a", "task_id": "t", "step_idx": 1,
            "screen_id": "/checkout", "category": "unexplained_error", "title": "Error message gives no cause",
            "description": "Error 'Error 0x3F' appears without a cause or a next step", "severity": 3,
            "source": "behavior", "failure_class": "UX_ISSUE", "status": "pending",
            "region_json": {"x": 30, "y": 130, "w": 200, "h": 40},
            "evidence_json": {"screenshot": shot, "steps": ["click 'Place order'"], "think_aloud": {}}}
    base.update(kw)
    return base


def make_step(idx, screen="/checkout", **signals):
    return {"idx": idx, "screen_id": screen, "signals_json": signals, "intended_json": signals.pop("action", {}),
            "frustration": signals.pop("fr", 0), "screenshot_path": signals.pop("shot", "")}
