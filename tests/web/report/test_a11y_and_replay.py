import io

from PIL import Image, ImageDraw

from proofloop.web.a11y import check_screen
from proofloop.web.report.replay_gif import issue_gif
from proofloop.web.runner.perception import perceive
from tests.fonts import font


def _png(draw_fn, size=(900, 400)):
    img = Image.new("RGB", size, "white")
    draw_fn(ImageDraw.Draw(img))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_contrast_is_measured_against_wcag_aa():
    def draw(d):
        d.text((20, 20), "Readable dark text", fill="#222222", font=font(20))
        d.text((20, 80), "Faint grey text here", fill="#bbbbbb", font=font(20))
        d.text((20, 140), "Placeholder grey ok", fill="#757575", font=font(20))
    found = check_screen(perceive(_png(draw)), screen_id="/", screenshot="", device="desktop", touch=False)
    flagged = {f.text.replace(" ", "") for f in found if f.check == "contrast"}   # OCR may drop spaces
    assert flagged == {"Faintgreytexthere"}
    faint = next(f for f in found if f.text.replace(" ", "") == "Faintgreytexthere")
    assert 1.7 <= faint.measured <= 2.3 and faint.required == 4.5 and "WCAG AA" in faint.detail


def test_small_tap_targets_only_count_on_touch_screens():
    def draw(d):
        d.rectangle([20, 16, 140, 38], fill="#1f5fbf")         # a 22 px tall button (label size varies by font)
        d.text((28, 19), "Buy now", fill="white", font=font(14))
        d.rectangle([20, 80, 200, 128], fill="#1f5fbf")        # a 48 px tall button
        d.text((40, 94), "Add to cart", fill="white", font=font(16))
    screen = perceive(_png(draw))
    touch = check_screen(screen, screen_id="/", screenshot="", device="mobile", touch=True)
    assert [f.text.replace(" ", "") for f in touch if f.check == "tap_target"] == ["Buynow"]   # OCR may drop spaces
    desk = check_screen(screen, screen_id="/", screenshot="", device="desktop", touch=False)
    assert not [f for f in desk if f.check == "tap_target"]


def test_replay_gif_ends_on_the_problem(tmp_path):
    shots = []
    for i, label in enumerate(["Cart", "Checkout", "Error 0x3F"]):
        p = tmp_path / f"{i}.png"
        Image.open(io.BytesIO(_png(lambda d, t=label: d.text((20, 20), t, fill="black", font=font(24))))).save(p)
        shots.append(p)
    steps = [{"idx": i, "screenshot_path": str(shots[i]), "intended_json": {"kind": "click", "x": 50, "y": 30},
              "think_json": {"does": f"I click {i}", "feels": "fine"}} for i in range(2)]
    out = issue_gif(steps, 1, tmp_path / "r.gif", region={"x": 15, "y": 15, "w": 140, "h": 40}, evidence=str(shots[2]))
    gif = Image.open(out)
    assert gif.n_frames == 3 and gif.info["loop"] == 0
    assert issue_gif([], 1, tmp_path / "none.gif") is None
