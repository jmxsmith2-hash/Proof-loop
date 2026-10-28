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


class OcrEngine:
    """Local OCR (RapidOCR / ONNX). One shared instance with a small result cache."""

    _instance: "OcrEngine | None" = None
    _lock = threading.Lock()

    def __init__(self, cache_size: int = 512):
        from rapidocr_onnxruntime import RapidOCR

        self._ocr = RapidOCR()
        self._cache: OrderedDict[str, list[tuple[list, str, float]]] = OrderedDict()
        self._cache_size = cache_size
        self._run_lock = threading.Lock()

    @classmethod
    def shared(cls) -> "OcrEngine":
        with cls._lock:
            if cls._instance is None:
                cls._instance = OcrEngine()
            return cls._instance

    def read(self, png: bytes) -> list[tuple[list, str, float]]:
        key = hashlib.sha1(png).hexdigest()
        with self._run_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            result, _ = self._ocr(png)
            out = [(box, text, float(conf)) for box, text, conf in (result or [])]
            self._cache[key] = out
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return out


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
                  dhash=dhash(image), gray_mean=float(lum.mean()))
