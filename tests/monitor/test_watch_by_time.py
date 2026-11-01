"""The failure watch looking every half minute or so instead of every few percent of the layers."""

from proofloop.core.settings import VerifyConfig
from proofloop.monitor.store import Store
from proofloop.monitor.vision import BedVerifier
from proofloop.monitor.watch import PrinterWatch
from tests.monitor.test_vision import jpeg, plate
from tests.monitor.test_vision_look import block, tangle
from tests.monitor.test_watch_spaghetti import CameraLink, alerts, growing, spaghetti


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def watching(tmp_path, frames, mode="alert", every_s=30, run_async=None):
    verifier = BedVerifier(tmp_path, VerifyConfig())
    verifier.add_reference(jpeg(plate(1)))
    link = CameraLink(frames)
    link.state.layer_num, link.state.total_layers = 1, 1000
    clock = Clock()
    w = PrinterWatch("p2s", link, verifier, Store(), settle_s=0, run_async=run_async or (lambda f: f()),
                     retry_s=0, failure_watch=lambda: mode, look_every=lambda: 10, look_every_s=lambda: every_s,
                     clock=clock)
    w.on_state(link.state)
    return w, link, clock


def every(w, link, clock, looks, seconds=30, layer=10):
    """``looks`` printer updates ``seconds`` apart, a layer further each time."""
    for _ in range(looks):
        clock.now += seconds
        layer += 1
        link.state.layer_num = layer
        w.on_state(link.state)
    return layer


def test_it_looks_every_30_seconds_not_at_every_update(tmp_path):
    w, link, clock = watching(tmp_path, [growing(n) for n in range(9)])
    link.state.layer_num = 10
    w.on_state(link.state)
    for _ in range(10):  # updates come every few seconds: no other look for 30 seconds
        clock.now += 2.9
        w.on_state(link.state)
    assert len(link.frames) == 8
    clock.now += 1
    w.on_state(link.state)
    assert len(link.frames) == 7
    every(w, link, clock, 7)
    assert not link.frames and not alerts(w)


def test_the_first_layers_are_left_alone(tmp_path):
    w, link, clock = watching(tmp_path, [growing(0)])
    for layer in (1, 2):
        clock.now += 60
        link.state.layer_num = layer
        w.on_state(link.state)
    assert len(link.frames) == 1
    link.state.layer_num = 3
    w.on_state(link.state)
    assert not link.frames


def test_a_tangle_must_last_a_minute_before_it_is_an_alert(tmp_path):
    frames = [growing(0), growing(1), spaghetti(2), spaghetti(3), spaghetti(4), spaghetti(5)]
    w, link, clock = watching(tmp_path, frames)
    every(w, link, clock, 4)  # two bad looks, 30 s apart: someone's hand, maybe
    assert not alerts(w)
    every(w, link, clock, 1, layer=14)  # still wrong a minute after the first
    (a,) = alerts(w)
    assert a["message"] == "the print looks like it has come loose or turned to spaghetti at layer 15 of 1000."
    every(w, link, clock, 1, layer=15)
    assert len(alerts(w)) == 1  # told once


def test_a_tangle_that_goes_away_is_not_an_alert(tmp_path):
    frames = [growing(0), growing(1), spaghetti(2), spaghetti(3), growing(4), spaghetti(5), spaghetti(6),
              growing(7)]
    w, link, clock = watching(tmp_path, frames)
    every(w, link, clock, 8)
    assert not alerts(w) and not link.frames


def test_two_bad_looks_are_enough_when_they_are_a_minute_apart(tmp_path):
    w, link, clock = watching(tmp_path, [growing(0), spaghetti(1), spaghetti(2)], every_s=120)
    every(w, link, clock, 3, seconds=120)
    assert len(alerts(w)) == 1


def test_pause_mode_pauses_the_printer(tmp_path):
    w, link, clock = watching(tmp_path, [growing(0)] + [spaghetti(n) for n in range(1, 4)], mode="pause")
    every(w, link, clock, 4)
    assert link.paused == 1 and w.waiting_for_you
    assert alerts(w)[0]["message"].endswith(". ProofLoop paused the printer.")


def test_a_tangle_that_grows_a_little_at_each_look_still_adds_up(tmp_path):
    def creeping(n):  # each look only a little more than the one before: next to it, that is fine
        return jpeg(tangle(block(plate(10 + n), (300, 250, 340, 290)), seed=3, strands=4 * n))

    frames = [creeping(n) for n in range(8)]
    w, link, clock = watching(tmp_path, frames)
    every(w, link, clock, 8)
    assert len(alerts(w)) == 1


def test_one_look_at_a_time_however_slow_the_camera(tmp_path):
    started = []
    w, link, clock = watching(tmp_path, [growing(0), growing(1)], run_async=started.append)
    every(w, link, clock, 3)  # the camera hasn't answered yet: no second look
    assert len(started) == 1
    started.pop()()
    every(w, link, clock, 1, layer=13)
    assert len(started) == 1


def test_the_tick_keeps_looking_when_the_printer_sends_nothing(tmp_path):
    w, link, clock = watching(tmp_path, [growing(0), growing(1)])
    link.state.layer_num = 10
    w.tick()
    clock.now += 30
    w.tick()
    assert not link.frames


def test_a_look_that_ends_after_its_print_is_dropped(tmp_path):
    started = []
    w, link, clock = watching(tmp_path, [spaghetti(0), spaghetti(1), spaghetti(2), spaghetti(3)],
                              run_async=started.append)
    every(w, link, clock, 1)
    w._new_loop()  # the next loop began while the camera was busy
    started.pop()()
    assert w._good_look is None and not w._looking


def test_halfway_and_at_80_percent_it_still_looks_for_an_empty_plate(tmp_path):
    empty = jpeg(plate(5))
    w, link, clock = watching(tmp_path, [growing(0), empty, empty])
    link.state.total_layers = 100
    every(w, link, clock, 1, layer=40)
    clock.now += 1  # long before the next look by time
    link.state.layer_num = 50
    w.on_state(link.state)
    link.state.layer_num = 80
    w.on_state(link.state)
    assert not link.frames
    (a,) = alerts(w)
    assert "printing layer 80 of 100, but the plate still looks empty" in a["message"]
