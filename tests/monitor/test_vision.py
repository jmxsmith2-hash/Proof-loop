import io

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance

from proofloop.core.settings import VerifyConfig
from proofloop.monitor.vision import BedVerifier, compare


def plate(seed=1, size=(640, 480)):
    """A textured PEI plate seen from above, with some fixed chamber clutter."""
    rng = np.random.default_rng(seed)
    base = np.full((size[1], size[0]), 90, dtype=np.float32)
    base += rng.normal(0, 4, base.shape)  # sensor noise
    yy, xx = np.mgrid[0 : size[1], 0 : size[0]]
    base += 25 * np.sin(xx / 37.0) * np.cos(yy / 53.0)  # plate texture / lighting
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8)).convert("RGB")
    ImageDraw.Draw(img).rectangle((0, 0, 640, 70), fill=(30, 30, 30))  # toolhead area, outside the ROI
    return img


def jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def test_same_plate_new_noise_is_clear():
    assert compare(plate(2), plate(1)).clear


def test_lighting_change_is_clear():
    darker = ImageEnhance.Brightness(plate(3)).enhance(0.8)
    assert compare(darker, plate(1)).clear


def test_part_on_plate_is_detected():
    img = plate(4)
    ImageDraw.Draw(img).rectangle((300, 250, 340, 290), fill=(230, 230, 230))
    res = compare(img, plate(1))
    assert not res.clear
    assert res.largest_blob_px > 30


def test_small_dark_part_is_detected():
    img = plate(5)
    ImageDraw.Draw(img).ellipse((200, 300, 222, 322), fill=(15, 15, 15))
    assert not compare(img, plate(1)).clear


def test_verifier_uses_best_reference(tmp_path):
    v = BedVerifier(tmp_path, VerifyConfig())
    v.add_reference(jpeg(ImageEnhance.Brightness(plate(1)).enhance(1.6)))
    v.add_reference(jpeg(plate(1)))
    res = v.check(jpeg(plate(6)))
    assert res.clear
    assert res.diff_png and res.diff_png.startswith(b"\x89PNG")
