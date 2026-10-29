"""Short animated replays of an issue: the persona's last steps up to the moment it happened.

Built from the session's own screenshots (nothing is re-run), with the click or
typing point marked and a caption saying what the persona did and thought.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

WIDTH = 640                 # frames are scaled to this width
CAPTION_H = 58
FRAME_MS = 1400
LAST_FRAME_MS = 3000


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int, lines: int = 2) -> list[str]:
    words, out, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
        else:
            out.append(line)
            line = w
            if len(out) == lines:
                break
    if len(out) < lines and line:
        out.append(line)
    if len(out) == lines and len(" ".join(out)) < len(text):
        out[-1] = out[-1].rstrip(".,;") + "..."
    return out


def frame(step: dict[str, Any], *, highlight: dict[str, int] | None = None, n: int = 0, total: int = 0) -> Image.Image:
    shot = Image.open(step["screenshot_path"]).convert("RGB")
    scale = WIDTH / shot.width
    img = shot.resize((WIDTH, round(shot.height * scale)), Image.LANCZOS)
    draw = ImageDraw.Draw(img)
    action = step.get("action_json") or step.get("intended_json") or {}
    if highlight:
        x, y, w, h = (round(highlight[k] * scale) for k in ("x", "y", "w", "h"))
        draw.rectangle([x, y, x + w, y + h], outline=(230, 40, 40), width=3)
    if action.get("x") is not None and action.get("kind") in ("click", "fill"):
        cx, cy = round(action["x"] * scale), round(action["y"] * scale)
        draw.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], outline=(230, 40, 40), width=3)
        draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(230, 40, 40))
    canvas = Image.new("RGB", (WIDTH, img.height + CAPTION_H), (24, 24, 27))
    canvas.paste(img, (0, 0))
    d = ImageDraw.Draw(canvas)
    think = step.get("think_json") or {}
    small, big = _font(13), _font(15)
    label = f"step {step['idx'] + 1}" + (f"  ({n}/{total})" if total else "")
    d.text((10, img.height + 6), label, fill=(160, 160, 170), font=small)
    quote = think.get("feels") or think.get("expects") or ""
    lines = _wrap(d, think.get("does") or _describe(action), big, WIDTH - 20, 1) + \
        (_wrap(d, f"“{quote}”", small, WIDTH - 20, 1) if quote else [])
    for i, line in enumerate(lines):
        d.text((10, img.height + 22 + i * 18), line, fill=(240, 240, 245) if i == 0 else (190, 190, 200),
               font=big if i == 0 else small)
    return canvas


def _describe(action: dict[str, Any]) -> str:
    kind = action.get("kind", "")
    target = action.get("target") or ""
    return f"{kind} {target!r}".strip() if target else kind


def issue_gif(steps: list[dict[str, Any]], issue_step: int, out: Path, *, region: dict[str, int] | None = None,
              evidence: str | None = None, frames: int = 6) -> Path | None:
    """GIF of the steps leading to `issue_step` (inclusive), ending on the screen where the problem shows.

    `evidence` is the screenshot the issue was found on (e.g. the error after a click); when it is not the
    issue step's own "before" screenshot, it becomes the last frame and carries the outline.
    """
    chosen = [s for s in steps if s["idx"] <= issue_step and s.get("screenshot_path")
              and Path(s["screenshot_path"]).is_file()][-frames:]
    if not chosen:
        return None
    issue = next((s for s in chosen if s["idx"] == issue_step), None)
    after = None
    if evidence and Path(evidence).is_file() and (issue is None or Path(evidence) != Path(issue["screenshot_path"])):
        after = {**(issue or chosen[-1]), "screenshot_path": evidence, "action_json": {}, "intended_json": {},
                 "think_json": {"does": "What happened next (the problem is outlined)"}}
    total = len(chosen) + (1 if after else 0)
    images = [frame(s, highlight=region if s["idx"] == issue_step and not after else None, n=i + 1, total=total)
              for i, s in enumerate(chosen)]
    if after:
        images.append(frame(after, highlight=region, n=total, total=total))
    height = max(i.height for i in images)
    images = [i if i.height == height else _pad(i, height) for i in images]
    palette = [i.convert("P", palette=Image.ADAPTIVE, colors=128) for i in images]
    out.parent.mkdir(parents=True, exist_ok=True)
    durations = [FRAME_MS] * (len(palette) - 1) + [LAST_FRAME_MS]
    palette[0].save(out, save_all=True, append_images=palette[1:], duration=durations, loop=0, optimize=True)
    return out


def _pad(img: Image.Image, height: int) -> Image.Image:
    canvas = Image.new("RGB", (img.width, height), (24, 24, 27))
    canvas.paste(img, (0, 0))
    return canvas
