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
