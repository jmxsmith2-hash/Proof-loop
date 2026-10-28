"""Bed-empty verification by comparing a chamber snapshot to reference images.

No ML and no cloud. For each printer you capture one or more pictures of the
empty plate (door open and closed, light on). A new snapshot is compared to
each reference inside a region of interest: brightness and contrast are matched
first so lighting drift does not count, then pixels that changed a lot are
grouped into blobs. A plate counts as clear only if the changed area is tiny and
no blob is big enough to be a part.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

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
    img = img.resize((WORK_WIDTH, max(1, int(h * scale))), Image.BILINEAR)
    img = img.filter(ImageFilter.GaussianBlur(1.5))
    w, h = img.size
    x0, y0, x1, y1 = roi
    box = (int(x0 * w), int(y0 * h), max(int(x0 * w) + 1, int(x1 * w)), max(int(y0 * h) + 1, int(y1 * h)))
    return np.asarray(img.crop(box), dtype=np.float32)


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
