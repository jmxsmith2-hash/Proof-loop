"""A TrueType font that exists on Linux, macOS and Windows (tests draw text for OCR)."""

from PIL import ImageFont

CANDIDATES = ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
              "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arial.ttf", "LiberationSans-Regular.ttf")


def font(size: int):
    for name in CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)
