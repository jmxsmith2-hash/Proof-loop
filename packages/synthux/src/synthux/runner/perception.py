"""Turn a screenshot into what a person could see: text, contrast, input boxes.

Pixels in, structured observations out. Nothing here touches the DOM.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from synthux.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class TextBox:
    text: str
    x0: int
    y0: int
    x1: int
    y1: int
    conf: float
    contrast: float = 21.0
    bg_lum: float = 1.0
    on_fill: bool = False          # sits on a coloured/filled background (looks like a button)
    fill_contrast: float = 1.0     # contrast of that filled background against the page

    @property
    def salience(self) -> float:
        """How visible the element is: its text, or the button shape it sits on, whichever stands out more."""
        return max(self.contrast, self.fill_contrast if self.on_fill else 1.0)

    @property
    def center(self) -> tuple[int, int]:
        return (self.x0 + self.x1) // 2, (self.y0 + self.y1) // 2

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def norm(self) -> str:
        return normalize(self.text)

    def region(self, pad: int = 4) -> dict[str, int]:
        return {"x": max(0, self.x0 - pad), "y": max(0, self.y0 - pad),
                "w": self.x1 - self.x0 + 2 * pad, "h": self.y1 - self.y0 + 2 * pad}


@dataclass
class InputBox:
    x0: int
    y0: int
    x1: int
    y1: int
    contents: list[TextBox] = field(default_factory=list)

    @property
    def center(self) -> tuple[int, int]:
        return (self.x0 + self.x1) // 2, (self.y0 + self.y1) // 2

    @property
    def text(self) -> str:
        return " ".join(t.text for t in self.contents)

    @property
    def empty(self) -> bool:
        return not self.contents

    def region(self) -> dict[str, int]:
        return {"x": self.x0, "y": self.y0, "w": self.x1 - self.x0, "h": self.y1 - self.y0}


@dataclass
class Screen:
    png: bytes
    width: int
    height: int
    texts: list[TextBox]
    inputs: list[InputBox]
    dhash: int
    gray_mean: float
    lum: np.ndarray | None = None   # per-pixel luminance (float32), for small-widget checks

    @property
    def all_text(self) -> str:
        return " ".join(t.text for t in self.texts)

    @property
    def norm_text(self) -> str:
        return normalize(self.all_text)

    def signature(self, top: int = 12) -> str:
        """A short, visible-text fingerprint used to recognise a screen on a return visit."""
        words = sorted({w for t in self.texts if t.height >= 14 for w in t.norm.split() if len(w) > 2})
        return " ".join(words[:top])

    def find(self, phrase: str, min_ratio: float = 0.82) -> list[TextBox]:
        return [t for t in self.texts if fuzzy_contains(t.norm, normalize(phrase), min_ratio)]


# ------------------------------------------------------------------- helpers

_OCR_FIXES = [(re.compile(r"\b0x", re.I), "0x"), (re.compile(r"\bOx(?=[0-9A-F])"), "0x")]


def normalize(text: str) -> str:
    text = text.replace("’", "'")
    text = re.sub(r"[^\w@.'+-]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip(" .")


def fuzzy_contains(haystack: str, needle: str, min_ratio: float = 0.82) -> bool:
    """True if `needle` appears in `haystack`, tolerating OCR errors and missing spaces."""
    if not needle:
        return False
    if needle in haystack or needle.replace(" ", "") in haystack.replace(" ", ""):
        return True
    words, target = haystack.split(), needle.split()
    n = len(target)
    for i in range(max(1, len(words) - n + 1)):
        window = " ".join(words[i:i + n])
        if difflib.SequenceMatcher(None, window, needle).ratio() >= min_ratio:
            return True
    return False


def fuzzy_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


_C = np.arange(256) / 255.0
_LINEAR_LUT = np.where(_C <= 0.04045, _C / 12.92, ((_C + 0.055) / 1.055) ** 2.4).astype(np.float32)


def luminance(rgb: np.ndarray) -> np.ndarray:
    """WCAG relative luminance per pixel (0..1)."""
    lin = _LINEAR_LUT[rgb.astype(np.uint8)]
    return 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]


def contrast_ratio(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def dhash(image: Image.Image, size: int = 8) -> int:
    small = np.asarray(image.convert("L").resize((size + 1, size), Image.BILINEAR), dtype=float)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hash_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def screen_diff(a: Screen, b: Screen) -> float:
    """Fraction of pixels that changed noticeably (0..1)."""
    ia = np.asarray(Image.open(io.BytesIO(a.png)).convert("L").resize((160, 100)), dtype=float)
    ib = np.asarray(Image.open(io.BytesIO(b.png)).convert("L").resize((160, 100)), dtype=float)
    return float(np.mean(np.abs(ia - ib) > 12))


# ----------------------------------------------------------------------- OCR


OCR_CACHE_VERSION = 1   # bump when OCR models or line recognition change


class OcrEngine:
    """Local OCR (RapidOCR / ONNX). One shared instance with two caches.

    * whole screenshots (an unchanged screen costs nothing);
    * single text lines: detection runs on every new screenshot, but a line whose
      pixels were already read (after a scroll, or when one field changed) is
      not recognised again. Recognition is most of the OCR time.

    Lines are recognised one at a time, so a line reads the same whatever else is
    on the screen (batching pads lines together and can change the spacing).
    """

    _instance: "OcrEngine | None" = None
    _lock = threading.Lock()

    def __init__(self, cache_size: int = 512, line_cache_size: int = 20_000):
        from rapidocr_onnxruntime import RapidOCR

        self._ocr = RapidOCR()
        for flag in ("use_angle_cls", "use_cls"):  # screen text is never upside down
            if hasattr(self._ocr, flag):
                setattr(self._ocr, flag, False)
        self._staged = all(hasattr(self._ocr, a) for a in
                           ("load_img", "text_detector", "sorted_boxes", "get_crop_img_list", "text_recognizer",
                            "filter_boxes_rec_by_score"))
        self._cache: OrderedDict[str, list[tuple[list, str, float]]] = OrderedDict()
        self._lines: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._cache_size = cache_size
        self._line_cache_size = line_cache_size
        self._run_lock = threading.Lock()
        self.stats = {"screens": 0, "screen_hits": 0, "lines": 0, "line_hits": 0}

    def save_lines(self, path: Path | str, keep: int = 50_000) -> int:
        """Keep recognised lines on disk so the next run on the same site reads repeated text for free."""
        import json

        with self._run_lock:
            items = list(self._lines.items())[-keep:]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps({"version": OCR_CACHE_VERSION, "lines": items}), encoding="utf-8")
        tmp.replace(path)
        return len(items)

    def load_lines(self, path: Path | str) -> int:
        import json

        p = Path(path)
        if not p.is_file():
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if data.get("version") != OCR_CACHE_VERSION:
            return 0  # recognised by a different OCR setup; start fresh
        with self._run_lock:
            for key, (text, conf) in data.get("lines", []):
                self._lines.setdefault(key, (text, float(conf)))
        return len(data.get("lines", []))

    @classmethod
    def shared(cls) -> "OcrEngine":
        with cls._lock:
            if cls._instance is None:
                cls._instance = OcrEngine()
            return cls._instance

    def read(self, png: bytes) -> list[tuple[list, str, float]]:
        key = hashlib.sha1(png).hexdigest()
        with self._run_lock:
            self.stats["screens"] += 1
            if key in self._cache:
                self.stats["screen_hits"] += 1
                self._cache.move_to_end(key)
                return self._cache[key]
            out = self._read_staged(png) if self._staged else self._read_plain(png)
            self._cache[key] = out
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return out

    def _read_plain(self, png: bytes) -> list[tuple[list, str, float]]:
        result, _ = self._ocr(png)
        return [(box, text, float(conf)) for box, text, conf in (result or [])]

    def _read_staged(self, png: bytes) -> list[tuple[list, str, float]]:
        try:
            ocr = self._ocr
            img = ocr.load_img(png)
            boxes, _ = ocr.text_detector(img)
            if boxes is None or len(boxes) == 0:
                return []
            boxes = ocr.sorted_boxes(boxes)
            crops = ocr.get_crop_img_list(img, boxes)
            recs = []
            for crop in crops:
                k = hashlib.sha1(crop.tobytes() + str(crop.shape).encode()).hexdigest()
                self.stats["lines"] += 1
                if k in self._lines:
                    self.stats["line_hits"] += 1
                    self._lines.move_to_end(k)
                else:
                    res, _ = ocr.text_recognizer([crop])
                    self._lines[k] = (res[0][0], float(res[0][1]))
                    if len(self._lines) > self._line_cache_size:
                        self._lines.popitem(last=False)
                recs.append(self._lines[k])
            kept_boxes, kept = ocr.filter_boxes_rec_by_score(boxes, recs)
            return [(b.tolist(), text, float(conf)) for b, (text, conf) in zip(kept_boxes, kept)]
        except Exception as exc:  # a RapidOCR version with different internals: use its own pipeline
            log.debug("staged OCR unavailable (%s); using the plain pipeline", exc)
            self._staged = False
            return self._read_plain(png)


def _text_box(raw_box: list, text: str, conf: float, lum: np.ndarray, page_lum: float = 1.0) -> TextBox:
    xs = [p[0] for p in raw_box]
    ys = [p[1] for p in raw_box]
    x0, x1, y0, y1 = int(min(xs)), int(max(xs)), int(min(ys)), int(max(ys))
    for pattern, repl in _OCR_FIXES:
        text = pattern.sub(repl, text)
    box = TextBox(text=text.strip(), x0=x0, y0=y0, x1=x1, y1=y1, conf=conf)
    h, w = lum.shape
    pad = 3
    crop = lum[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)]
    if crop.size == 0:
        return box
    border = np.concatenate([crop[0, :], crop[-1, :], crop[:, 0], crop[:, -1]])
    bg = float(np.median(border))
    diff = np.abs(crop - bg)
    cutoff = np.quantile(diff, 0.9)
    fg_pixels = crop[diff >= max(cutoff, 1e-6)]
    fg = float(np.mean(fg_pixels)) if fg_pixels.size else bg
    box.contrast = round(contrast_ratio(fg, bg), 2)
    box.bg_lum = round(bg, 3)
    box.on_fill = bg < 0.75
    box.fill_contrast = round(contrast_ratio(bg, page_lum), 2)
    return box


# --------------------------------------------------------------- input boxes


def _segments(dark: np.ndarray, min_w: int) -> list[tuple[int, int, int, int]]:
    """Thin horizontal line segments: (y_top, y_bottom, x0, x1)."""
    counts = dark.sum(axis=1)
    done: list[list[int]] = []
    active: list[list[int]] = []
    for y in np.where(counts >= min_w)[0]:
        y = int(y)
        row = np.concatenate([[0], dark[y].astype(np.int8), [0]])
        d = np.diff(row)
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        keep = [seg for seg in active if seg[1] >= y - 1]
        done.extend(seg for seg in active if seg[1] < y - 1)
        active = keep
        for s, e in zip(starts.tolist(), ends.tolist()):
            if e - s < min_w:
                continue
            for seg in active:
                if seg[1] == y - 1 and abs(seg[2] - s) <= 6 and abs(seg[3] - e) <= 6:
                    seg[1], seg[2], seg[3] = y, min(seg[2], s), max(seg[3], e)
                    break
            else:
                active.append([y, y, s, e])
    done.extend(active)
    done.sort()
    return [tuple(s) for s in done if s[1] - s[0] <= 4]  # thin lines only (borders, not filled blocks)


def detect_input_boxes(lum: np.ndarray, min_w: int = 90, min_h: int = 26, max_h: int = 80) -> list[InputBox]:
    dark = lum < 0.6  # border pixels clearly darker than a light background
    segs = _segments(dark, min_w)
    boxes: list[InputBox] = []
    for i, top in enumerate(segs):
        for bottom in segs[i + 1:]:
            gap = bottom[0] - top[1]
            if not (min_h <= gap <= max_h):
                continue
            if abs(top[2] - bottom[2]) > 8 or abs(top[3] - bottom[3]) > 8:
                continue
            x0, x1 = min(top[2], bottom[2]), max(top[3], bottom[3])
            inner = dark[top[1] + 2: bottom[0] - 1]
            if inner.size == 0:
                continue
            left = inner[:, max(0, x0 - 4): x0 + 5].any(axis=1).mean()
            right = inner[:, max(0, x1 - 5): x1 + 4].any(axis=1).mean()
            interior = inner[:, x0 + 6: x1 - 6]
            fill = lum[top[1] + 3: bottom[0] - 2, x0 + 6: x1 - 6]
            light_inside = fill.size > 0 and float(np.median(fill)) >= 0.9  # inputs are white inside; banners are tinted
            if left > 0.7 and right > 0.7 and interior.size and interior.mean() < 0.25 and light_inside:
                boxes.append(InputBox(x0=x0, y0=top[0], x1=x1, y1=bottom[1]))
                break
    return boxes


# -------------------------------------------------------------------- screen


async def perceive_async(png: bytes) -> Screen:
    """perceive() off the event loop, so other sessions' timing keeps running during OCR."""
    import asyncio

    return await asyncio.to_thread(perceive, png)


def perceive(png: bytes, ocr: OcrEngine | None = None) -> Screen:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    rgb = np.asarray(image)
    lum = luminance(rgb)
    engine = ocr or OcrEngine.shared()
    page_lum = float(np.median(lum))
    texts = [_text_box(b, t, c, lum, page_lum) for b, t, c in engine.read(png) if t.strip()]
    texts.sort(key=lambda t: (t.y0 // 10, t.x0))
    inputs = detect_input_boxes(lum)
    for box in inputs:
        box.contents = [t for t in texts if t.x0 >= box.x0 - 2 and t.x1 <= box.x1 + 2
                        and t.y0 >= box.y0 - 2 and t.y1 <= box.y1 + 2]
    return Screen(png=png, width=image.width, height=image.height, texts=texts, inputs=inputs,
                  dhash=dhash(image), gray_mean=float(lum.mean()), lum=lum.astype(np.float32))


def checkbox_left_of(screen: Screen, text: TextBox) -> tuple[int, int, bool] | None:
    """A small square box just left of a label: (centre x, centre y, looks checked)."""
    if screen.lum is None:
        return None
    lum = screen.lum
    cy = text.center[1]
    x1 = max(0, text.x0 - 2)
    x0 = max(0, text.x0 - 44)
    y0, y1 = max(0, cy - 18), min(lum.shape[0], cy + 18)
    region = lum[y0:y1, x0:x1]
    if region.size == 0:
        return None
    dark = region < 0.55
    rows, cols = np.where(dark)
    if rows.size < 12:
        return None
    # take the right-most dark blob (closest to the text)
    right = cols.max()
    blob = dark[:, max(0, right - 30): right + 1]
    r2, c2 = np.where(blob)
    h = r2.max() - r2.min() + 1
    w = c2.max() - c2.min() + 1
    if not (9 <= h <= 32 and 9 <= w <= 32 and 0.7 <= w / h <= 1.4):
        return None
    inner = blob[r2.min() + 3: r2.max() - 2, c2.min() + 3: c2.max() - 2]
    checked = bool(inner.size and inner.mean() > 0.25)
    bx = x0 + max(0, right - 30) + (c2.min() + c2.max()) // 2
    by = y0 + (r2.min() + r2.max()) // 2
    return int(bx), int(by), checked


def menu_icons(screen: Screen, top_fraction: float = 0.22) -> list[tuple[int, int]]:
    """Centres of "hamburger" menu icons (three short, equal, stacked bars) near the top of the screen.

    Phones hide the site menu behind this icon, which has no text for OCR to read.
    """
    if screen.lum is None:
        return []
    from scipy import ndimage

    band = screen.lum[: max(40, int(screen.lum.shape[0] * top_fraction))]
    background = ndimage.median_filter(band, size=(19, 1))           # local colour above/below each pixel
    ink = np.abs(band - background) > 0.3
    labels, n = ndimage.label(ink)
    bars = []
    for i, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if 2 <= h <= 6 and 14 <= w <= 44 and (labels[sl] == i).mean() >= 0.8:
            bars.append((sl[0].start, sl[0].stop, sl[1].start, sl[1].stop))
    texts = [(t.x0 - 2, t.y0 - 2, t.x1 + 2, t.y1 + 2) for t in screen.texts]
    icons = []
    bars.sort()
    for a in range(len(bars)):
        stack = [bars[a]]
        for b in bars[a + 1:]:
            last = stack[-1]
            gap = b[0] - last[1]
            if abs(b[2] - last[2]) <= 3 and abs(b[3] - last[3]) <= 3 and 2 <= gap <= 12:
                if len(stack) == 1 or abs(gap - (last[0] - stack[-2][1])) <= 2:
                    stack.append(b)
            if len(stack) == 3:
                break
        if len(stack) != 3:
            continue
        cx = (stack[0][2] + stack[0][3]) // 2
        cy = (stack[0][0] + stack[2][1]) // 2
        if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in texts):
            continue  # part of text (e.g. "≡" in a word), not an icon
        if not any(abs(cx - x) < 10 and abs(cy - y) < 10 for x, y in icons):
            icons.append((int(cx), int(cy)))
    return icons
