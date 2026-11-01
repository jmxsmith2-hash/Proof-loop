"""Loop files: Make a loop file reads an example plate and shows what it found."""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

from tests.browser.window import EXAMPLES  # noqa: E402


def test_make_a_loop_file_reads_the_example_plate_and_shows_its_analysis(window):
    page = window.tab("loopfiles", "make")
    plate = EXAMPLES / "bracket_plate.gcode.3mf"
    page.locator("#makeFile").set_input_files(plate)

    expect(page.locator("#makeResult")).to_be_visible()
    expect(page.locator("#makeError")).to_be_hidden()
    expect(page.locator("#plateTitle")).to_have_text(plate.name)
    expect(page.locator("#plateChip")).to_have_text("ready")
    facts = page.locator("#plateFacts")
    for fact in ("P2S", "PLA", "Textured PEI Plate", "28 min"):  # printer, filament, plate, print time
        expect(facts).to_contain_text(fact)
    # the two brackets, each with the height the pusher reaches it at
    rows = page.locator("#partTable tr").filter(has_text="Part 10")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("Part 102")
    expect(rows.nth(1)).to_contain_text("Part 101")
    expect(page.locator("#topView svg")).to_be_visible()
    expect(page.locator("#sideView svg")).to_be_visible()
    panel = page.locator('[data-panel="make"]')
    expect(panel.get_by_role("heading", name="One loop, step by step")).to_be_visible()
    expect(panel.get_by_text("FarmBoard reset", exact=True)).to_be_visible()
    expect(panel.get_by_text("Plate check", exact=True)).to_be_visible()


def test_a_file_that_is_not_a_plate_is_refused_with_a_message(window, tmp_path):
    window.expect_console_error("Failed to load resource")  # the refused upload, as the browser logs it
    page = window.tab("loopfiles", "make")
    junk = tmp_path / "not_a_plate.3mf"
    junk.write_bytes(b"this is not a zip file")
    page.locator("#makeFile").set_input_files(junk)
    expect(page.locator("#makeError")).to_be_visible()
    expect(page.locator("#makeResult")).to_be_hidden()


def test_a_file_with_two_sliced_plates_offers_both_and_adds_them_to_the_batch(window, tmp_path):
    from tests.plates import two_plates

    page = window.tab("loopfiles", "make")
    plate = (EXAMPLES / "bracket_plate.gcode.3mf").read_bytes()
    pair = tmp_path / "pair.gcode.3mf"
    pair.write_bytes(two_plates(plate, plate))
    page.locator("#makeFile").set_input_files(pair)

    expect(page.locator("#plateTitle")).to_have_text("pair-plate1.gcode.3mf")
    pick = page.locator("#platePick")
    expect(pick).to_contain_text("2 sliced plates in this file")
    pick.get_by_role("button", name="Plate 2").click()
    expect(page.locator("#plateTitle")).to_have_text("pair-plate2.gcode.3mf")
    expect(pick.get_by_role("button", name="Plate 2")).to_have_attribute("aria-pressed", "true")
    pick.get_by_role("button", name="+ Add all 2 to the batch").click()
    rows = page.locator("#batchRows .batch-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("pair-plate1.gcode.3mf")
    expect(rows.nth(1)).to_contain_text("pair-plate2.gcode.3mf")
    expect(page.locator("#batchOut")).to_contain_text("of printing plus cooldowns")
    expect(page.locator("#batchOut .ic.danger")).to_have_count(0)  # the two plates go together in one file
    expect(pick.get_by_role("button", name="✓ All in the batch")).to_be_disabled()


def test_the_send_panel_shows_which_printer_is_ready_and_turns_sending_on_in_place(window):
    page = window.tab("loopfiles", "make")
    page.locator("#makeFile").set_input_files(EXAMPLES / "bracket_plate.gcode.3mf")
    expect(page.locator("#makeResult")).to_be_visible()
    box = page.locator("#sendBox")
    expect(box).to_be_visible()
    targets = box.locator(".send-target")
    expect(targets).to_have_count(3)  # the three pretend printers, each ready or saying why not
    toggle = page.locator("#sendToggle")
    was_on = toggle.is_checked()  # the pretend farm starts with sending on
    set_sending = "(el, on) => { el.checked = on; el.dispatchEvent(new Event('change')); }"
    if was_on:
        toggle.evaluate(set_sending, False)
    expect(box.locator("#sendOff")).to_be_visible()
    expect(box.locator("#sendBtn")).to_be_disabled()
    box.get_by_role("button", name="Turn on sending to printers").click()
    expect(box.locator("#sendOff")).to_be_hidden()
    first = targets.first  # the pretend printers are all printing: each says so, and none can be picked
    expect(first).to_contain_text("is busy (running)")
    expect(first).to_contain_text("PLA → A1")  # the AMS tray that would feed the plate's filament
    expect(first.locator("input")).to_be_disabled()
    expect(box.locator("#sendBtn")).to_be_disabled()
    toggle.evaluate(set_sending, was_on)  # as the farm had it
    if was_on:
        expect(box.locator("#sendOff")).to_be_hidden()
    else:
        expect(box.locator("#sendOff")).to_be_visible()
