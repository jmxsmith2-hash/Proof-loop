"""Looking at a print in progress: a part that grows is fine, a tangle all over the plate is not."""

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageEnhance

from proofloop.core.settings import VerifyConfig
from proofloop.monitor.vision import BedVerifier, PrintLook, judge_look, print_look
from tests.monitor.test_vision import jpeg, plate

EMPTY = plate(1)


def block(img, xy, fill=(230, 230, 230)):
    ImageDraw.Draw(img).rectangle(xy, fill=fill)
    return img


def tangle(img, seed=7, strands=300, width=4, box=(160, 180, 520, 460)):
    """Strands of light and dark filament criss-crossing a wide part of the plate."""
    rng = np.random.default_rng(seed)
    draw = ImageDraw.Draw(img)
    for _ in range(strands):
        points = [
            (int(rng.integers(box[0], box[2])), int(rng.integers(box[1], box[3])))
            for _ in range(int(rng.integers(3, 7)))
        ]
        c = int(rng.choice([235, 240, 25, 200]))
        draw.line(points, fill=(c, c // 2 + 60, c), width=width)
    return img


def look(img):
    return print_look(img, EMPTY)


def judge(now, before):
    return judge_look(look(now), look(before) if before is not None else None)


def part(seed, xy=(300, 250, 340, 290)):
    return block(plate(seed), xy)


def test_a_part_that_grows_is_fine():
    assert judge(part(3, (295, 230, 345, 292)), part(2)).state == "ok"
    assert judge(part(3, (280, 200, 365, 292)), part(2)).state == "ok"  # grown by half, still one piece


def test_a_whole_plate_of_parts_growing_is_fine():
    def parts(seed, grow):
        img = plate(seed)
        for i in range(5):
            for j in range(3):
                x, y = 60 + i * 70, 200 + j * 70
                block(img, (x - grow, y - grow, x + 40 + grow, y + 40 + grow))
        return img

    assert judge(parts(3, 5), parts(2, 0)).state == "ok"


def test_the_toolhead_moving_over_the_plate_is_fine():
    head = block(part(3), (380, 120, 480, 200), (40, 40, 40))  # one big dark piece that wasn't there
    assert judge(head, part(2)).state == "ok"


def test_a_tangle_across_the_plate_is_bad():
    for strands in (150, 300, 600):
        t = judge(tangle(part(4), strands=strands), part(2))
        assert t.state == "bad", (strands, t)
        assert t.new_area > 0.2 and t.spread > 0.5


def test_a_tangle_with_no_part_before_it_is_bad():
    assert judge(tangle(plate(5), seed=9), part(2)).state == "bad"


def test_a_part_dragged_loose_leaving_a_wide_trail_is_bad():
    img = part(3)
    draw = ImageDraw.Draw(img)
    for k in range(40):
        draw.line([(320, 270), (320 - k * 10, 360 + k % 5 * 10), (180 + k * 6, 440)], fill=(225, 225, 225), width=3)
    assert judge(img, part(2)).state == "bad"


def test_a_lamp_getting_dimmer_is_fine():
    dim = ImageEnhance.Brightness(part(3)).enhance(0.6)
    assert judge(dim, part(2)).state == "ok"


def test_the_first_look_has_nothing_to_compare_with():
    assert judge(tangle(part(4)), None).state == "ok"


def test_a_blank_or_dark_picture_is_not_judged():
    for flat in (Image.new("RGB", (640, 480), (120, 120, 120)), Image.new("RGB", (640, 480), (0, 0, 0))):
        with pytest.raises(ValueError):
            look(flat)


def test_garbage_is_not_judged():
    rng = np.random.default_rng(5)
    noise = Image.fromarray(rng.integers(0, 256, (480, 640, 3), dtype=np.uint8))
    with pytest.raises(ValueError):
        look(noise)


def test_a_change_over_the_whole_plate_is_unclear_not_bad():
    everything = PrintLook(np.ones((168, 224), dtype=bool), np.ones((168, 224), dtype=bool))
    assert judge_look(everything, look(part(2))).state == "unclear"


def test_the_verifier_looks_against_the_closest_empty_picture(tmp_path):
    v = BedVerifier(tmp_path, VerifyConfig())
    v.add_reference(jpeg(ImageEnhance.Brightness(plate(1)).enhance(1.6)))
    v.add_reference(jpeg(plate(1)))
    after = v.look(jpeg(tangle(part(4))))
    assert after.reference.startswith("ref-0") and after.area > 0.2
    assert judge_look(after, v.look(jpeg(part(2)))).state == "bad"
    with pytest.raises(RuntimeError):
        BedVerifier(tmp_path / "none", VerifyConfig()).look(jpeg(part(2)))
