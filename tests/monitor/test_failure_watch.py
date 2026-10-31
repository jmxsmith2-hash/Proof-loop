"""The failure watch: part-way through a print the plate must no longer look empty."""

from proofloop.monitor.store import Store
from proofloop.monitor.watch import PrinterWatch
from tests.monitor.test_monitor import Link, Verifier


class PausingLink(Link):
    paused = 0

    def pause(self):
        self.paused += 1
        return True


def watching(results, mode="alert", refs=1):
    link = PausingLink()
    link.state.layer_num, link.state.total_layers = 1, 100
    w = PrinterWatch("p2s", link, Verifier(results, refs), Store(), settle_s=0, run_async=lambda f: f(),
                     retry_s=0, failure_watch=lambda: mode)
    w.on_state(link.state)
    return w, link


def print_to(w, link, layer):
    link.state.layer_num = layer
    w.on_state(link.state)


def alerts(w):
    return [e for e in w.store.events() if e["kind"] == "failure_watch"]


def test_an_empty_plate_at_both_looks_is_an_alert():
    w, link = watching([True, True])
    for layer in range(2, 101):
        print_to(w, link, layer)
    (a,) = alerts(w)
    assert a["level"] == "error" and "printing layer 80 of 100, but the plate still looks empty" in a["message"]
    assert not link.paused and not w.waiting_for_you


def test_a_part_on_the_plate_is_fine():
    w, link = watching([False, True])  # halfway the part is there; later a look happens to see nothing
    for layer in range(2, 101):
        print_to(w, link, layer)
    assert not alerts(w)


def test_pause_mode_pauses_the_printer_and_waits_for_you():
    w, link = watching([True, True], mode="pause")
    for layer in range(2, 101):
        print_to(w, link, layer)
    assert link.paused == 1 and w.waiting_for_you and "ProofLoop paused the printer" in alerts(w)[0]["message"]


def test_it_never_looks_when_off_without_pictures_or_not_printing():
    for w, link in (watching([], mode="off"), watching([], refs=0)):
        for layer in range(2, 101):
            print_to(w, link, layer)  # Verifier([]) would fail if it were asked
        assert not alerts(w)
    w, link = watching([])
    link.state.gcode_state = "PAUSE"
    print_to(w, link, 90)
    assert not alerts(w)


def test_each_loop_is_watched_afresh():
    w, link = watching([True, True, True, True])
    for layer in range(2, 101):
        print_to(w, link, layer)
    w._new_loop()
    for layer in range(1, 101):
        print_to(w, link, layer)
    assert len(alerts(w)) == 2


def test_after_a_restart_past_both_looks_each_still_gets_its_own_picture():
    w, link = watching([True, True])  # ProofLoop started again at layer 85: both marks are behind it
    print_to(w, link, 85)
    assert not alerts(w)  # one picture can't be both looks
    print_to(w, link, 86)
    assert len(alerts(w)) == 1


def test_a_pause_proofloop_made_for_a_failure_is_never_resumed_even_on_the_last_layer():
    w, link = watching([True, True, True, True], mode="pause")
    for layer in range(2, 100):
        print_to(w, link, layer)
    assert link.paused == 1
    link.state.layer_num, link.state.gcode_state = 100, "PAUSE"  # the pause lands on the loop's last layer
    w.on_state(link.state)
    assert link.resumed == 0 and w.waiting_for_you and "may have failed" in w.status
    link.state.gcode_state = "RUNNING"  # you pressed Resume: the next loop-end pause is the loop's own again
    w.on_state(link.state)
    assert not w._failure_paused
