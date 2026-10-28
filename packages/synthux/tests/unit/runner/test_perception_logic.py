import numpy as np

from synthux.runner.perception import (contrast_ratio, detect_input_boxes, fuzzy_contains, hash_distance,
                                       luminance, normalize)


def test_contrast_ratio_matches_wcag():
    white = float(luminance(np.array([[[255, 255, 255]]], dtype=np.uint8))[0, 0])
    black = float(luminance(np.array([[[0, 0, 0]]], dtype=np.uint8))[0, 0])
    grey = float(luminance(np.array([[[211, 211, 211]]], dtype=np.uint8))[0, 0])
    assert round(contrast_ratio(white, black), 1) == 21.0
    assert 1.3 < contrast_ratio(white, grey) < 1.6   # the demo's ghost button


def test_fuzzy_contains_tolerates_ocr_noise():
    assert fuzzy_contains(normalize("TrailBackpack3OLand"), "trail backpack")
    assert fuzzy_contains(normalize("Createaccount"), "create account")
    assert fuzzy_contains(normalize("Account created. You can now log in."), "account created")
    assert not fuzzy_contains("contact", "continue")


def test_detects_bordered_input_but_not_filled_button_or_tinted_banner():
    img = np.full((300, 600), 1.0, dtype=np.float32)
    img[50:52, 100:400] = 0.1; img[90:92, 100:400] = 0.1   # input box borders
    img[50:92, 100:102] = 0.1; img[50:92, 398:400] = 0.1
    img[150:190, 100:300] = 0.2                              # filled button
    img[220:222, 100:400] = 0.1; img[262:264, 100:400] = 0.1  # banner with tinted inside
    img[220:264, 100:102] = 0.1; img[220:264, 398:400] = 0.1
    img[222:262, 102:398] = 0.8
    boxes = detect_input_boxes(img)
    assert [(b.x0, b.y0) for b in boxes] == [(100, 50)]


def test_hash_distance():
    assert hash_distance(0b1010, 0b1001) == 2
