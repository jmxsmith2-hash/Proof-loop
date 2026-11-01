"""Bed-empty verification by comparing a chamber snapshot to reference images.

No ML and no cloud. For each printer you capture one or more pictures of the
empty plate (door open and closed, light on). A new snapshot is compared to
each reference inside a region of interest: brightness and contrast are matched
first so lighting drift does not count, then pixels that changed a lot are
grouped into blobs. A plate counts as clear only if the changed area is tiny and
no blob is big enough to be a part.

During a print the same pictures answer a different question: has it gone wrong?
``print_look`` records where the plate differs from the empty plate, and ``judge_look``
compares that with the last look that was fine: a print gone to spaghetti or dragged loose
shows as a large area of new change, away from where the part was, that is fragmented, spread
out and full of edges, while a growing part is one compact piece next to what was there. It is
a heuristic (see docs/printers.md); one look is never enough to alert (watch.py).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage

WORK_WIDTH = 320


@dataclass
class BedCheck:
    clear: bool
    changed_fraction: float
    largest_blob_px: int
    reference: str
    diff_png: bytes | None = None

    def as_dict(self) -> dict:
        return {
            "clear": self.clear,
            "changed_fraction": round(self.changed_fraction, 5),
            "largest_blob_px": self.largest_blob_px,
            "reference": self.reference,
        }


def _prepare(img: Image.Image, roi: tuple[float, float, float, float]) -> np.ndarray:
    img = img.convert("L")
    w, h = img.size
    scale = WORK_WIDTH / w
    img = img.resize((WORK_WIDTH, max(1, int(h * scale))), Image.Resampling.BILINEAR)
    img = img.filter(ImageFilter.GaussianBlur(1.5))
    w, h = img.size
    x0, y0, x1, y1 = roi
    box = (int(x0 * w), int(y0 * h), max(int(x0 * w) + 1, int(x1 * w)), max(int(y0 * h) + 1, int(y1 * h)))
    return np.asarray(img.crop(box), dtype=np.float32)


def _match(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``a`` with its brightness and contrast matched to ``b``, fitted on the half of the pixels that
    differ least, so a big tangle doesn't pass for a change of light."""
    r = np.abs(a - b - np.median(a - b))
    keep = r <= np.percentile(r, 50)
    x, y = b[keep], a[keep]
    gain = float(np.cov(x, y)[0, 1] / (x.var() or 1.0)) if x.std() > 1.0 else 1.0
    if gain < 0.4:  # a dark, flat or blanked picture: matching it would only blow up noise
        raise ValueError("the camera picture is too dark or flat to judge")
    gain = min(gain, 2.5)
    return (a - float(y.mean() - gain * x.mean())) / gain


def _largest_blob(mask: np.ndarray) -> int:
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best = 0
    for sy, sx in zip(*(a.tolist() for a in np.nonzero(mask)), strict=True):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        n = 0
        while stack:
            y, x = stack.pop()
            n += 1
            for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= yy < h and 0 <= xx < w and mask[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True
                    stack.append((yy, xx))
        best = max(best, n)
    return best


def compare(
    snapshot: Image.Image,
    reference: Image.Image,
    roi=(0.15, 0.25, 0.85, 0.95),
    pixel_threshold: int = 38,
    max_changed_fraction: float = 0.004,
    min_blob_px: int = 30,
    reference_name: str = "",
    want_diff: bool = False,
) -> BedCheck:
    a = _prepare(snapshot, roi)
    b = _prepare(reference, roi)
    if a.shape != b.shape:
        h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]
    # Match the snapshot's brightness and contrast to the reference so a dimmer
    # LED or auto-exposure drift is not mistaken for a part.
    a = (a - a.mean()) / (a.std() or 1.0) * (b.std() or 1.0) + b.mean()
    diff = np.abs(a - b)
    mask = diff > pixel_threshold
    # Morphological opening drops single-pixel sensor noise and JPEG artefacts.
    m = (
        Image.fromarray((mask * 255).astype(np.uint8))
        .filter(ImageFilter.MinFilter(3))
        .filter(ImageFilter.MaxFilter(3))
    )
    mask = np.asarray(m) > 0
    fraction = float(mask.mean())
    blob = _largest_blob(mask)
    diff_png = None
    if want_diff:
        buf = io.BytesIO()
        Image.fromarray(np.clip(diff * 2, 0, 255).astype(np.uint8)).save(buf, "PNG")
        diff_png = buf.getvalue()
    clear = fraction <= max_changed_fraction and blob < min_blob_px
    return BedCheck(clear, fraction, blob, reference_name, diff_png)


# ---- looking at a print in progress ------------------------------------------------------
# All sizes are in the 320 px wide working picture (WORK_WIDTH); areas are shares of the plate area.
NEAR_PX = 6  # growth of the part, its shadow and a shaky camera count as "where it already was" up to here
NEW_MIN = 0.04  # new change smaller than this is never a tangle
PIECE_MIN = 6  # a piece of new change smaller than this is noise
GLITCH_AREA = 0.85  # change over nearly the whole plate is a lamp, a bumped camera or a torn frame
EDGE_MIN = 8.0  # a change in brightness steeper than this (levels per pixel) is an edge
# A look is bad when the new change is wide-spread (TANGLE_SPREAD of the plate) and either large and
# stringy, or simply huge. Stringy: ragged (fills little of its bounding box), full of edges or in pieces.
TANGLE_SPREAD, TANGLE_LARGE, TANGLE_HUGE = 0.25, 0.12, 0.35
TANGLE_FILL, TANGLE_TEXTURE, TANGLE_STRAY = 0.6, 0.3, 0.35
EIGHT = np.ones((3, 3), dtype=bool)


@dataclass
class PrintLook:
    """One picture of a print in progress, reduced to where it differs from the empty plate."""

    mask: np.ndarray  # changed pixels
    edges: np.ndarray  # pixels where the change has an edge: a tangle is full of them, a solid part isn't
    reference: str = ""

    @property
    def area(self) -> float:
        return float(self.mask.mean())


@dataclass
class Tangle:
    """What a look says about the print. ``state``: "ok", "bad", or "unclear" (a glitch: counts as neither)."""

    state: str
    new_area: float = 0.0  # change away from where the last good look had the part
    pieces: int = 0
    stray: float = 0.0  # share of that change outside its biggest piece
    spread: float = 0.0  # share of the plate its bounding box covers
    fill: float = 0.0  # share of that box it fills: a tangle is ragged, a part is solid
    texture: float = 0.0  # share of it that is edge

    def as_dict(self) -> dict:
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in self.__dict__.items()}


def print_look(
    snapshot: Image.Image,
    reference: Image.Image,
    roi=(0.15, 0.25, 0.85, 0.95),
    pixel_threshold: int = 38,
    reference_name: str = "",
) -> PrintLook:
    a = _prepare(snapshot, roi)
    b = _prepare(reference, roi)
    if a.shape != b.shape:
        h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]
    diff = np.abs(_match(a, b) - b)
    mask = diff > pixel_threshold
    labels, n = ndimage.label(mask, structure=EIGHT)
    if n:  # no opening here: that would take thin strands away too
        mask = np.bincount(labels.ravel())[labels] >= 4
        mask &= labels > 0
    edges = np.hypot(*np.gradient(diff)) > EDGE_MIN
    return PrintLook(mask, edges, reference_name)


def judge_look(look: PrintLook, base: PrintLook | None) -> Tangle:
    """``base`` is the last look that was fine (None for the first look: nothing to compare with)."""
    if base is None or base.mask.shape != look.mask.shape:
        return Tangle("ok")
    if look.area >= GLITCH_AREA:
        return Tangle("unclear")
    new = look.mask & ~ndimage.binary_dilation(base.mask, iterations=NEAR_PX)
    new_area = float(new.mean())
    if new_area < NEW_MIN:
        return Tangle("ok", new_area)
    labels, _ = ndimage.label(new, structure=EIGHT)
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    big = sizes >= PIECE_MIN
    kept = big[labels]
    pieces = int(big.sum())
    if not pieces:
        return Tangle("ok", new_area)
    ys, xs = np.nonzero(kept)
    stray = 1.0 - float(np.max(sizes)) / float(kept.sum())
    spread = float((ys.max() - ys.min() + 1) * (xs.max() - xs.min() + 1)) / new.size
    fill = float(kept.sum()) / new.size / spread
    texture = float(look.edges[kept].mean())
    stringy = fill <= TANGLE_FILL or texture >= TANGLE_TEXTURE or stray >= TANGLE_STRAY
    bad = spread >= TANGLE_SPREAD and (new_area >= TANGLE_HUGE or (new_area >= TANGLE_LARGE and stringy))
    return Tangle("bad" if bad else "ok", new_area, pieces, stray, spread, fill, texture)


class BedVerifier:
    """Holds the reference images for one printer."""

    def __init__(self, ref_dir: str | Path, verify_cfg):
        self.dir = Path(ref_dir)
        self.cfg = verify_cfg

    @property
    def references(self) -> list[Path]:
        return sorted(self.dir.glob("ref-*.jpg")) if self.dir.exists() else []

    def add_reference(self, jpeg: bytes) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        Image.open(io.BytesIO(jpeg)).verify()
        n = len(self.references)
        path = self.dir / f"ref-{n:02d}.jpg"
        path.write_bytes(jpeg)
        return path

    def clear_references(self) -> None:
        for p in self.references:
            p.unlink()

    def check(self, jpeg: bytes, want_diff: bool = True) -> BedCheck:
        refs = self.references
        if not refs:
            raise RuntimeError("no reference image of the empty plate yet; capture one first")
        snap = Image.open(io.BytesIO(jpeg))
        best: BedCheck | None = None
        for ref in refs:
            with Image.open(ref) as r:
                res = compare(
                    snap,
                    r,
                    self.cfg.roi,
                    self.cfg.pixel_threshold,
                    self.cfg.max_changed_fraction,
                    self.cfg.min_blob_px,
                    ref.name,
                    want_diff,
                )
            if best is None or (res.changed_fraction, res.largest_blob_px) < (
                best.changed_fraction,
                best.largest_blob_px,
            ):
                best = res
        assert best is not None
        return best

    def look(self, jpeg: bytes) -> PrintLook:
        """Where a picture of a print in progress differs from the empty plate (the closest reference)."""
        refs = self.references
        if not refs:
            raise RuntimeError("no reference image of the empty plate yet; capture one first")
        snap = Image.open(io.BytesIO(jpeg))
        best: PrintLook | None = None
        for ref in refs:
            with Image.open(ref) as r:
                res = print_look(snap, r, self.cfg.roi, self.cfg.pixel_threshold, ref.name)
            if best is None or res.area < best.area:
                best = res
        assert best is not None
        return best
