"""Each loop repeats the start G-code: its tunes can be turned off, and the load line is drawn once."""

import re

from proofloop.loopfiles import threemf
from proofloop.loopfiles.gcode.processor import process
from proofloop.loopfiles.gcode.routines import LoopSettings
from proofloop.loopfiles.profiles import ProfileStore
from proofloop.monitor.service import loop_settings
from tests.plates import SMALL, make_3mf, plate_gcode

P = ProfileStore().get("p2s-sweep-only")
TUNE = "M1006 S1\nM1006 A53 B9 L99 C53 D9 M99 E53 F9 N99\nM1006 W\n"
# As in a P2S file from Bambu Studio 02.04 (see a real one: ";===== noozle load line end" is Bambu's typo).
LOAD_LINE = """;===== printer start sound ===================
{tune}  M83
  G1 E-3 F1800
;===== nozzle load line ===============================
M1002 gcode_claim_action : 51
  G29.2 S1 ; ensure z comp turn on
  G90
  M83
  M109 S220
  G130 O0 X100 Y-0.4 Z0.8 F8.31497 L40 E20 D5
  ;G130 O0 X100 Y-0.2 Z0.6 F8.31497 L40 E12 D4
  G90
  M83
  G1 Z0.2
  M400
;===== noozle load line end ===========================
"""


def bambu_plate() -> threemf.SlicedFile:
    gcode = plate_gcode(SMALL).replace("; purge line, not an object\n", LOAD_LINE.format(tune=TUNE), 1)
    gcode = gcode.replace("; MACHINE_END_GCODE_START\n", "; MACHINE_END_GCODE_START\n;=====printer finish  sound=========\n"
                          + TUNE, 1)
    return threemf.loads(make_3mf(gcode, SMALL), name="boot.gcode.3mf")


def loops(text: str) -> list[str]:
    return re.split(r"; === PROOFLOOP LOOP \d+ OF \d+ ===\n", text)


def tunes(body: str) -> int:
    return len(re.findall(r"^\s*M1006\b", body, re.MULTILINE))


def draws(body: str) -> int:
    return len(re.findall(r"^\s*G130\b", body, re.MULTILINE))


def test_the_load_line_is_drawn_on_the_first_loop_only_and_later_loops_prime_with_a_dab():
    text = process(bambu_plate(), P, LoopSettings(loops=3)).gcode
    first, *later = loops(text)
    assert len(later) == 2 and draws(first) == 1
    for body in later:
        assert draws(body) == 0 and "; (load line drawn on the first loop only) G130 O0 X100 Y-0.4" in body
        dab = body[body.index("; (load line drawn"):]
        # back to where the line starts, the 3 mm pulled back by the start G-code and half a mm more, then aside
        assert "G1 X100 Y0 F12000\nG1 Z0.8 F600\nG1 E3.5 F150" in dab and "G1 Z2 F600\nG1 X105 F3000\n" in dab
        block = body[body.index(";===== nozzle load line"):body.index("noozle load line end")]
        # the rest of the block still runs: hot nozzle before the dab, first-layer height after it
        assert block.index("M109 S220") < block.index("G1 E3.5") < block.index("G1 X105") < block.index("G1 Z0.2")
    assert tunes(first) == tunes(later[-1]) == 2 * 3  # sounds stay as sliced unless you change it


def test_the_load_line_can_be_kept_on_every_loop():
    text = process(bambu_plate(), P, LoopSettings(loops=3, load_line_every_loop=True)).gcode
    assert [draws(b) for b in loops(text)] == [1, 1, 1]


def test_the_printer_sounds_can_play_only_at_the_end_or_not_at_all():
    off = process(bambu_plate(), P, LoopSettings(loops=3, sounds="off")).gcode
    assert tunes(off) == 0 and off.count("; (silenced by ProofLoop) M1006") == 3 * 2 * 3
    last = loops(process(bambu_plate(), P, LoopSettings(loops=3, sounds="last")).gcode)
    assert [tunes(b) for b in last] == [0, 0, 3]  # only the finish tune of the last loop
    end = last[-1]
    assert end.index("M1006 S1", end.index("printer finish")) > end.index("; MACHINE_END_GCODE_START")


def test_the_settings_come_from_your_defaults():
    s = loop_settings({"sounds": "off", "load_line_every_loop": True})
    assert s.sounds == "off" and s.load_line_every_loop
    d = loop_settings({})
    assert d.sounds == "every" and not d.load_line_every_loop
