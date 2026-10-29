from synthux.physical import feedback
from synthux.physical.cad.checks import CadFinding


def finding(check="overhang", severity=3):
    return CadFinding(check=check, severity=severity, title="Overhang", detail="45° and more")


def test_two_false_alarms_lower_a_check_on_that_printer_only(tmp_path):
    path = tmp_path / "feedback.json"
    feedback.record(path, "overhang", "Bambu Lab P2S", "PLA", "wrong")
    f = [finding()]
    assert feedback.adjust(f, "Bambu Lab P2S", "PLA", path) == 0 and f[0].severity == 3  # once isn't enough
    assert feedback.record(path, "overhang", "Bambu Lab P2S", "PLA", "wrong") == {"wrong": 2, "right": 0}
    f = [finding(), finding("thin_wall")]
    assert feedback.adjust(f, "Bambu Lab P2S", "PLA", path) == 1
    assert f[0].severity == 2 and "false alarm 2 times" in f[0].detail and f[1].severity == 3
    other = [finding()]
    assert feedback.adjust(other, "Bambu Lab P2S", "PETG", path) == 0  # another material: unchanged
    feedback.record(path, "overhang", "Bambu Lab P2S", "PLA", "right")
    feedback.record(path, "overhang", "Bambu Lab P2S", "PLA", "right")
    again = [finding()]
    assert feedback.adjust(again, "Bambu Lab P2S", "PLA", path) == 0  # confirmed as often: back to normal


def test_four_false_alarms_make_it_a_note_but_never_for_loop_safety(tmp_path):
    path = tmp_path / "feedback.json"
    for check in ("overhang", "loop_file"):
        for _ in range(4):
            feedback.record(path, check, "P", "M", "wrong")
    f = [finding(severity=4), finding("loop_file", 4)]
    feedback.adjust(f, "P", "M", path)
    assert f[0].severity == 1 and f[1].severity == 4
