"""The failure watch's looks for a print gone wrong: compact growth is fine, a tangle twice in a row alerts."""

from proofloop.core.settings import VerifyConfig
from proofloop.monitor.store import Store
from proofloop.monitor.vision import BedVerifier
from proofloop.monitor.watch import PrinterWatch, look_marks
from tests.monitor.test_failure_watch import PausingLink
from tests.monitor.test_vision import jpeg, plate
from tests.monitor.test_vision_look import block, tangle


def growing(n):
    """The print as it looks at look number ``n``: one block that grows."""
    return jpeg(block(plate(10 + n), (300 - 3 * n, 250 - 3 * n, 340 + 3 * n, 290 + 3 * n)))


def spaghetti(n):
    return jpeg(tangle(block(plate(10 + n), (300, 250, 340, 290)), seed=n))


class CameraLink(PausingLink):
    """Gives the pictures it is told to, one for each look; an Exception in the list is a camera error."""

    def __init__(self, frames):
        super().__init__()
        self.frames = list(frames)

    def snapshot(self):
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return frame


def watching(tmp_path, frames, mode="alert", every=10):
    verifier = BedVerifier(tmp_path, VerifyConfig())
    verifier.add_reference(jpeg(plate(1)))
    link = CameraLink(frames)
    link.state.layer_num, link.state.total_layers = 1, 100
    shown = []
    w = PrinterWatch("p2s", link, verifier, Store(), lambda e, image=None: shown.append((e, image)),
                     settle_s=0, run_async=lambda f: f(), retry_s=0, failure_watch=lambda: mode,
                     look_every=lambda: every)
    w.on_state(link.state)
    return w, link, shown


def print_through(w, link, layers=range(2, 101)):
    for layer in layers:
        link.state.layer_num = layer
        w.on_state(link.state)


def told(shown):
    """What was sent to your notification targets for the failure watch: (event, picture)."""
    return [(e, image) for e, image in shown if e["kind"] == "failure_watch"]


def alerts(w):
    return [e for e in w.store.events() if e["kind"] == "failure_watch"]


def test_the_looks_are_every_so_many_percent_and_always_at_half_and_80():
    assert look_marks(0) == (0.5, 0.8)
    assert look_marks(10) == (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
    assert look_marks(25) == (0.25, 0.5, 0.75, 0.8)
    assert look_marks(15) == (0.15, 0.3, 0.45, 0.5, 0.6, 0.75, 0.8, 0.9)


def test_a_compact_growing_part_is_fine(tmp_path):
    w, link, shown = watching(tmp_path, [growing(n) for n in range(9)])
    print_through(w, link)
    assert not alerts(w) and not link.frames and not told(shown)


def test_a_tangle_in_two_looks_in_a_row_is_an_alert_with_the_picture(tmp_path):
    frames = [growing(n) for n in range(5)] + [spaghetti(5), spaghetti(6)] + [spaghetti(7), spaghetti(8)]
    w, link, shown = watching(tmp_path, frames)
    print_through(w, link)
    (a,) = alerts(w)  # told once, not at every look after
    assert a["level"] == "error"
    assert a["message"] == "the print looks like it has come loose or turned to spaghetti at layer 70 of 100."
    assert a["data"]["new_area"] > 0.2
    assert not link.paused and not w.waiting_for_you
    ((_, image),) = told(shown)
    assert image == frames[6]  # the picture of the look that told you


def test_one_bad_look_is_not_an_alert(tmp_path):
    frames = [growing(0), growing(1), spaghetti(2), growing(3), growing(4), spaghetti(5), growing(6),
              growing(7), growing(8)]
    w, link, _ = watching(tmp_path, frames)
    print_through(w, link)
    assert not alerts(w)


def test_pause_mode_pauses_the_printer(tmp_path):
    frames = [growing(n) for n in range(4)] + [spaghetti(4), spaghetti(5)]
    w, link, _ = watching(tmp_path, frames, mode="pause")
    print_through(w, link, range(2, 61))
    assert link.paused == 1 and w.waiting_for_you
    assert alerts(w)[0]["message"].endswith("at layer 60 of 100. ProofLoop paused the printer.")
    assert "paused" in w.status


def test_a_camera_that_fails_or_gives_garbage_is_never_an_alert(tmp_path):
    frames = [growing(0), growing(1), OSError("camera went away"), RuntimeError("no frame"), growing(4),
              b"not a picture", b"\xff\xd8 broken", growing(7), growing(8)]
    w, link, _ = watching(tmp_path, frames)
    print_through(w, link)
    assert not alerts(w) and not link.frames


def test_a_blank_picture_between_two_tangles_does_not_end_the_run(tmp_path):
    from PIL import Image

    flat = jpeg(Image.new("RGB", (640, 480), (0, 0, 0)))
    frames = [growing(0), growing(1), spaghetti(2), flat, spaghetti(4)]
    w, link, _ = watching(tmp_path, frames)
    print_through(w, link, range(2, 51))
    assert len(alerts(w)) == 1


def test_it_does_not_look_for_a_tangle_when_set_to_only_the_two_empty_plate_looks(tmp_path):
    w, link, _ = watching(tmp_path, [spaghetti(5), spaghetti(8)], every=0)
    print_through(w, link)
    assert not alerts(w) and not link.frames


def test_it_does_not_look_when_off_or_when_not_printing(tmp_path):
    w, link, _ = watching(tmp_path, [spaghetti(1)] * 9, mode="off")
    print_through(w, link)
    assert not alerts(w) and len(link.frames) == 9
    w, link, _ = watching(tmp_path / "paused", [spaghetti(1)] * 9)
    link.state.gcode_state = "PAUSE"
    print_through(w, link)
    assert len(link.frames) == 9


def test_each_print_is_looked_at_afresh(tmp_path):
    frames = [growing(0), growing(1), spaghetti(2), spaghetti(3)] + [growing(0), growing(1), spaghetti(2), spaghetti(3)]
    w, link, _ = watching(tmp_path, frames)
    print_through(w, link, range(2, 41))
    w._new_loop()
    print_through(w, link, range(1, 41))
    assert len(alerts(w)) == 2
